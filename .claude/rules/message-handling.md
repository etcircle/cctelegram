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

## Per-user output verbosity + post-turn digest collapse (plan v4)

`handlers/output_prefs.resolve(user_id)` is the single per-recipient
verbosity authority (stored `/settings` choice > explicitly-set legacy env
default > preset; a stored PRESET choice overrides the entire env layer).
Production default preset is `standard`; the TEST SUITE pins `verbose`
(≡ pre-settings behavior) in conftest so the scenario floor stays
today-shaped. Digest renderers take per-recipient line/snippet/live-line
budgets; quiet (`digest_card=False`) never creates digest state (including
the Agent counter path — images + attention-dismiss still fire).

**W1 collapse-on-done** (`digest_on_done`): at `_finalize_activity_digest`,
`summary` (default) collapses the activity card to ONE line — run-state
header (a post-turn 🔔 survives) + tool/sub-agent counts + duration, all
frozen on state at finalize so repaints are edit-stable; `keep` is today's
full card; `delete` removes the card via the cancellation-safe protocol:
both debounce schedulers shield the LOCK-HOLDING flush (a cancel only ever
lands in the sleep), the upsert re-checks tombstone + slot identity under
the lock before any send, and the finalize-delete takes the lock,
tombstones, deletes best-effort (a RetryAfter never wedges content), and
pops the slot — no resurrection by `refresh_activity_digest_if_present` or
the poller repaint. Restart-mid-protocol orphan = accepted residual
(digest state is in-memory, matching today's restart behavior).

**W2 sub-agent collapse** (`subagent_cards`): the sidechain's own
end-of-turn — a final visible text whose `MessageTask.stop_reason` (plumbed
from `NewMessage`) is end-turn — triggers the synchronous
`_collapse_subagent_digest` (cancel pending debounce, render the one-line
`↳ Sub-agent · xxx ✅ N tools` under the per-key lock, `last_text` =
collapsed render). `_finalize_activity_digest` is the BACKSTOP sweep for
empty-final sidechains (`lifecycle_only` end markers never reach the
display path). The collapsed slot is a tombstone: late re-detected blocks
never re-inflate the play-by-play; a new run has a new key. `off` never
creates a card. The 🤖✅ report message (full, expandable) is untouched at
every policy; sidechain keep-alive (Wave A) fires from session_monitor and
is unaffected. **Fix 5 (ISSUE-6): the Workflow sub-agent shape rides this
SAME contract** — it collapses on its own `end_turn`+`text` (path 1), via
the unchanged parent-finalize backstop (path 2), AND via a new deterministic
**route-FIFO close collapse** (path 3: the `<task-notification>` close marks
the bracket `closing`, `check_sidechain_updates` tails the final tail then
emits a `NewMessage(subagent_collapse_prefix)` → `enqueue_subagent_collapse`
→ a summary-gated `subagent_collapse` control task) that guarantees an
empty-final Workflow card collapses even when paths 1/2 can't fire.

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
`interactive_pending`, `notification_pending`, `notification_set_at`,
`notification_generation`, `notification_clear_reason`, `background_agents`.
The two
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

**Notification-set `WAITING_ON_USER` (Workflow / permission approval "🔔 Waiting
on you" — Wave B)** — the SECOND lower-authority derivation input,
`notification_pending`, beside the pane bit above. A Workflow/permission
approval gate blocks Claude WITH its (non-interactive) `tool_use` open and no
JSONL trace, so the route sat `RUNNING_TOOL` ("🟡 Busy") forever. The Claude
Code `Notification` hook writes `notify_pending/<session_id>.json`
(`{ts, window_key, generation, kind}` — NO message text);
`handlers/notify_source.py` is the trust boundary (HARD
`window_key == "tmux_session:window_id"` read predicate — a double-`--resume`
sibling never lights; schema + future-skew validation; deliberately NO
read-TTL). The poller consumes it at the TOP of the per-binding path
(`_consume_notification_signal`, BEFORE the transition repaint and the
adaptive capture gating — a capture-skipped tick still consumes, and a 🔔
transition repaints the digest the SAME tick). `mark_notification_pending`
returns a `NotificationMarkResult` that DRIVES the generation-guarded unlink
(committed-live → unlink AFTER the commit; redundant-transcript-waiting /
stale-unlinked → unlink; ignored-no-unlink → never unlink, never seed).
Deriver precedence: transcript-interactive open id > `notification_pending`
(over ANY open_tools, incl. the open Workflow id, or empty) >
`pane_interactive_pending` (empty only) > RUNNING_TOOL > RUNNING — the two
bits clear INDEPENDENTLY and the pane bit's contract is untouched. The ONE
idle exception: IDLE(pane) with a non-empty `suspended_tools` stash is
positive live proof the pane clear was false — the mark RESTORES the stash
and derives WAITING (the second stash-restore path). CLEAR: a transcript
`user` event unconditionally; `tool_result` / end-of-turn / task-notification
events only when their JSONL timestamp is strictly NEWER than
`notification_set_at` (None/older preserves — buffered pre-notification JSONL
must not re-hide the wait; a preserved bit at end-of-turn keeps WAITING
instead of idling; an unknown `tool_result` preserves). **Fix 1 (ISSUE-5 arm
A): plain assistant `text`/`thinking` narration NO LONGER clears the bit** —
a Workflow blocked on an approval gate narrates *while* blocked, and the
buffered-flush timestamp is not causal order vs the gate, so a newer
narration block must not bury the wait; the narration branches call
`_clear_notification_if_setat_invalid` (the corrupt `set_at=None` invariant
repair ONLY), never the causal `_maybe_clear_notification_by_ts`. The poller's
pane-RUNNING observation at a
capture taken strictly after `set_at + NOTIFY_PANE_CLEAR_MARGIN_S` (LEVEL +
margin, NOT an idle→active edge — the adaptive capture can skip the blocked
approval frame, so an edge requirement strands the bit when the last
pre-notification capture was already running; the blocked prompt replaces
the run chrome, so a status-active frame sufficiently after the hook fired
is positive proof the user approved, and the margin keeps a same-tick
capture of the pre-prompt frame from clearing early); the
`NOTIFY_TTL_SECONDS` (30 min) runtime TTL evaluated from the
SNAPSHOT every tick independent of side-file existence (pending-without-
set_at = invariant violation = expired); and route teardown
(`mark_session_reset` / `clear_route` / `clear_routes_for_topic`). Side-file
lifecycle: unlinked per the mark result, on session replacement / `/clear`
(OLD session id) / topic close, 24h startup GC with the injected
`is_live_session` conservative-skip. Pull-only; no observer (c313657 stays
forbidden).

**Notification clear-reason channel + durable decision card (ISSUE-5 Fix
3a/3b/3c/3d).** Every `notification_pending` True→False transition stamps a
typed `NotificationClearReason` (`USER` / `TOOL_RESULT` / `END_OF_TURN` /
`TASK_NOTIFICATION` / `INVARIANT` / `PANE_RUNNING` / `TTL` / `TEARDOWN`),
surfaced on the snapshot as `notification_clear_reason` (`_clear_notification_in_place`
takes a REQUIRED `reason`; `mark_notification_cleared(route, *, reason)` — the
poller passes `TTL` / `PANE_RUNNING`; reset to None on each fresh commit). The
🔔 now drives a **persistent, audible decision card** (`attention.notify_waiting(...,
kind="notification_decision")` → the "🔔 Claude needs a decision" header; NO
notification text stored — privacy). The poller posts it on `COMMITTED_LIVE`
BEFORE the side-file unlink, gated by `interactive_ui.has_interactive_surface`
(Fix 3d — never double-cards over a live AUQ/EPM surface; gate on the surface,
NOT the pane bit). `status_polling._reconcile_decision_card` runs at the END of
every consume: **retry-while-pending** (re-post idempotently while
`notification_pending`, so a transient first-post failure never strands the
route on the silent digest header); **KEEP** while cleared with reason
`END_OF_TURN` AND a live `background_agents` key still projects Busy (the
EOT-gap — a 🔔 raised by a Workflow's own approval gate survives the parent's
end-of-turn); **DISMISS** kind-aware (`attention.dismiss_if_kind(...,
kind="notification_decision")`) on every other reason. **EOT-gap grace (codex
P2):** the monitor applies the parent end-of-turn (clearing 🔔) DURING
`check_for_updates` but the same-batch Workflow launch (the bg key) only via
the later `apply_sidechain_activity` fan-out, so a reconcile can land in
between (bit cleared, bg key not yet visible) and dismiss prematurely — the
END_OF_TURN-with-empty-bg dismiss is therefore HELD for
`DECISION_CARD_EOT_GRACE_S` (poller-local `_decision_card_eot_grace` deadline)
so a lagging launch becomes visible; only after the grace elapses with still no
key (a genuine no-workflow end-of-turn) is it dismissed. **Dismiss audit (Fix
3c):** every generic display-layer `attention.dismiss` (`message_queue` ×4,
`interactive_ui` clear_interactive_msg, `inbound_telegram` user-reply) became
`dismiss_if_kind("interactive_ui")` so display-path cleanup / narration can
NEVER ack a `notification_decision` card — the decision card dismisses ONLY via
the reason-driven poller path (the genuine-user dismissal flows through the
route_runtime `user` clear → reason `USER` → reconcile). `AttentionState.set_at`
is a WALL stamp. Pull-only; no observer (c313657 stays forbidden).

**Background-agent projected Busy (GH #44 — typing + 🟡 while a
`run_in_background` agent works).** A background async agent keeps writing its
sidechain for minutes-to-hours after the parent's authoritative end-of-turn,
with its output visibly streaming into the topic — but sidechain blocks are
display-path `NewMessage`s, never lifecycle events, so the route used to
render idle (no typing) the whole time. The fix is a THIRD lower-authority
route_runtime input, `background_agents`, applied as a **snapshot-time
PROJECTION**: the stored `run_state` is never mutated on an agent's account;
the single snapshot builder lifts a stored-idle route with a live
(non-expired, non-tombstoned) key to a visible RUNNING — `typing_eligible`,
the digest header, and /dashboard all follow from the snapshot. Precedence:
a committed `notification_pending` projects WAITING_ON_USER above the lift
(user-action-needed beats machine-busy), and `mark_notification_pending` now
COMMITS on stored-idle + a live background key (the second idle exception
beside the pane-stash resurrect) so a 🔔 raised by the background agent's own
approval gate is never stale-dropped. **Keys** (always through
`utils.normalize_background_agent_key` — agentId == sidechain stem minus
`agent-` == task-id): `mark_background_agent_activity(route, key, max_ts)` is
the keyed Wave A successor (heartbeat + UNqualified pane-false-idle
resurrection preserved verbatim; a NEW key on a stored-idle route records
ONLY when `event_ts > last_assistant_turn_ended_at`, both non-None, strict —
a buffered pre-end-of-turn flush fails closed; active/WAITING recording is
unconditional but foreground-presumed); `mark_background_agent_launched`
registers `is_background=True` from the parent's async-launch tool_result
(the structured `agentId:` line is the anchor; the prose sentence is
diagnostic-only) so the key survives the parent's end-of-turn regardless of
sidechain batching. **Clears**: `mark_background_agent_done` on the agent's
own sidechain end-of-turn (lifecycle-only markers included) and on the
parent's `<task-notification>` task-id (extracted monitor-side, applied
after lifecycle dispatch); the `BG_AGENT_TTL_SECONDS` (30 min) wall-clock
heartbeat TTL (`_wall_now()` injectable; expire-before-classify deletes a
stale record before NEW/EXISTING classification so a late None-ts batch can
never relift); the provenance-only foreground prune at the authoritative
end-of-turn (synchronous agents always finish before their parent's turn
ends — `is_background` keys are NEVER pruned); and route teardown. Done keys
are TOMBSTONED — reset only on a GENUINE user turn. A task-notification user
event (`TranscriptLifecycleEvent.is_task_notification`, stamped by the
adapter via the public `response_builder.is_task_notification`) is
machine-initiated: it counts as activity but preserves the pane bit, the
stash, and the tombstones, clears the notification bit timestamp-qualified
only, and RE-DERIVES with the preserved gates (never a forced RUNNING — the
`interactive_pending ⟺ pane-set WAITING` invariant holds). The status CARD
stays pane-driven and may clear on the idle pane while the lift holds —
typing + digest/dashboard Busy are the contracted surfaces (recorded product
decision). Restart degradation: all in-memory; the stamp-None guard keeps
post-restart sidechain batches from lifting (no false Busy), so the route
renders idle until fresh parent activity. Pull-only throughout (no observer;
c313657 stays forbidden).

**Workflow-tool bracket (ISSUE-6 — extends GH #44 to the `Workflow` tool).**
GH #44 only detected the `Agent` tool's `run_in_background` (`agentId:` launch +
single-level `subagents/agent-*.jsonl` glob); the `Workflow` tool has a
DIFFERENT shape (subagents one level deeper at `subagents/workflows/wf_*/`, a
launch tool_result with `Task ID:` mid-line and a separate `Run ID`, and a
`<task-notification>` close keyed by the Task ID), so a Workflow run rendered
idle (no typing). The fix reuses the SAME `background_agents` machinery via a
**parent-transcript bracket** keyed `wf-task:<task_id>` (passes
`normalize_background_agent_key` as identity — no `agent-` prefix — so it never
aliases the Agent/Task namespace). **Launch anchor = STRUCTURED-primary (PR-2):**
the launch parse reads the ENTRY-level `toolUseResult`
(`{status:"async_launched", taskId, runId, transcriptDir, …}`, plumbed onto the
tool_result `ParsedEntry` as `tool_result_meta` by `transcript_parser`) via
`response_builder.workflow_launch_info_from_meta` — the robust,
version-drift-proof source; `transcriptDir` IS the validated `wf_dir` (no
run-id-topology derivation, no glob). It keys on the Workflow fields (`taskId`),
NEVER on `status` alone — the Agent/Task `run_in_background` async launch ALSO
carries `status=="async_launched"` but a DIFFERENT shape (`agentId`, no
`taskId`; verified 54-vs-40 in the JSONL history) and must return None.
`response_builder.extract_workflow_launch_info` (regex `(?im)^.*\bTask ID:\s*…` —
Task ID is MID-LINE, verified against real launches; the captured id ==
the `<task-notification>` close key, the open/close parity invariant) is the
PROSE FALLBACK, used ONLY when the structured field is genuinely ABSENT
(`tool_result_meta is None`: older Claude Code / a future whole-field rename /
a non-dict coerced to None) and logged with a WARNING for drift detectability.
A PRESENT structured dict that does not parse as an async_launched Workflow is
AUTHORITATIVE — the prose is NOT consulted (so a stale/quoted `Task ID:` line
can't open a bogus bracket; hermes P2). NOTE: this structured-primary anchor is
the LIVE-MONITOR path only — the PR-1 startup reconciler
`_scan_workflow_launches_and_closes` (below) stays PROSE-only by design (a
disclosed follow-up: widening its `Task ID` byte-prefilter to `async_launched`
to read the structured field there would JSON-parse the common Agent
async-launch lines and turn one malformed line into a fail-closed no-lift for an
unrelated live Workflow). `session_monitor` adds the raw
`wf-task:<id>` to `.launched` (→ `mark_background_agent_launched`,
`is_background=True`, survives the parent end-of-turn prune → typing + 🟡) and
opens a persistent `_WorkflowBracket`. **Fix 2c heartbeat (DESIGN B — separate
channel):** each poll, `_emit_workflow_bracket_heartbeats` stats the bracket's
`wf_dir` for the freshest `*.jsonl` mtime and emits a `wf-task:<id>` refresh
into `ParentSidechainActivity.bracket_heartbeats` (→ `mark_background_agent_activity`)
ONLY on an mtime ADVANCE (real new sidechain writes) — never by parsing
sidechain ENTRIES (run-state consumes only the bracket + a dir stat); no new
writes → the key ages out via the 30-min `BG_AGENT_TTL_SECONDS` (the dead/
never-completed backstop); a `wf_dir`-less bracket never heartbeats (ages out
one TTL from `launch_wall`). **Close = GATE-ON-BRACKET ONLY:** the
`<task-notification>` emits the `wf-task:<id>` close key (→
`mark_background_agent_done` tombstone) IFF a live open bracket exists — never
guessing a Workflow id from its character set; an isolated close with no
bracket has no route_runtime key to tombstone, so the bare key suffices.
Out-of-order done-before-launch fail-closes (the done tombstone no-ops the
later launch). The bracket is now MARKED `closing` (not popped immediately) so
the Fix 5 display path tails its `wf_dir` one final time before teardown (see
below); `_emit_workflow_bracket_heartbeats` skips closing brackets. This
`wf-task:` key is ALSO what makes ISSUE-5 arm B fire: a stored-idle route with
a live `wf-task:` key lets `mark_notification_pending` re-commit (§3.6) instead
of STALE_UNLINK, so a 🔔 raised by the Workflow's own approval gate is durable.

**BUSY restart reconciler (PR-1 Half B — re-arm typing + 🟡 + ↳ from the
filesystem after `launchctl kickstart`).** All the bracket / `background_agents`
state above is IN-MEMORY, so a restart of a still-running Workflow renders the
topic idle until a fresh parent turn — the owner's highest-frequency symptom.
`session_monitor._reconcile_workflow_brackets_on_startup(current_map)` runs ONCE
in `_monitor_loop` startup (beside `_hydrate_ask_tool_input_cache`, before the
poll loop): for each tracked parent with NO live open bracket (idempotency —
skip a parent that already has one), STAT-glob
`<project>/<parent_sid>/subagents/workflows/wf_*` (anchored, never `rglob`) and,
for any `wf_*` dir whose freshest `*.jsonl` mtime is within
`_RECONCILE_FRESH_WINDOW_S` (1800s, mirrors `BG_AGENT_TTL_SECONDS` without
importing route_runtime), recover its Task ID + close-state from ONE bounded
parent-JSONL scan (`_scan_workflow_launches_and_closes` — the
`_auq_tool_result_present` byte-prefilter pattern, matching the launch's Run ID /
Transcript-dir basename to `wf_dir.name`; fail-closed `({}, set())` on any read
error). **Three-state rule:** (1) task_id recovered + NO `<task-notification>`
close → LIFT: reopen a `_WorkflowBracket` (steady-state heartbeat + Fix-5 ↳
display resume) AND emit the raw `wf-task:<id>` into
`_parent_activity(sid).launched` — the bot fan-out
(`apply_sidechain_activity` → `route_runtime.seed_idle_and_mark_background_agent_
launched`) SEEDS the unseeded parent route IDLE and lifts it to projected
RUNNING (the B1-FIX: a bare `mark_background_agent_launched` would no-op on the
unseeded route); (2) close FOUND → NO runtime lift (a Workflow that finished just
before the deploy must not false-relight) — open a DISPLAY-ONLY `closing` bracket
for the final ↳ tail + collapse, then it's dropped; (3) task_id UNRECOVERABLE /
scan failed → DO NOT LIFT (fail-closed — prefer dark-until-next-turn over a false
🟡). STAT-only discovery (the parent JSONL is read ONLY when a fresh `wf_*` dir
exists — the cost-bound property), a per-tick `_RECONCILE_MAX_WF_DIRS` cap (16),
and the whole pass try/except-guarded so it can never break startup. No-reflood:
a reopened bracket's sub-files resume from the persisted `monitor_state.json`
offset and a first-seen post-restart file starts at EOF
(`_track_and_emit_sidechain_file`), so pre-restart ↳ blocks never replay. The
steady-state idle-route re-scan (B3b) is deferred — the startup pass covers the
post-kickstart symptom. Pull-only; no observer.

**Fix 5 (ISSUE-6 owner decision #2 — SHIPPED): the `↳` sub-agent DISPLAY cards
for Workflow sidechains.** A Workflow's sub-agents live one level deeper at
`subagents/workflows/wf_<runid>/agent-*.jsonl`, so a single-level glob missed
them. `check_sidechain_updates` adds a SECOND, anchored
`bracket.wf_dir.glob("agent-*.jsonl")` enumeration over THIS parent's OPEN
brackets (the SAME `wf_dir` the heartbeat stats — one shared discovery), driven
through the shared `_track_and_emit_sidechain_file(..., feed_run_state=False)`
helper so Workflow sidechain ENTRIES NEVER feed run-state (the `wf-task:`
bracket + mtime heartbeat stay the SOLE Workflow run-state input — `ticks` stays
empty, `route_runtime`/`apply_sidechain_activity`/`_finalize_activity_digest`
UNCHANGED). The tracking key is run-id-qualified `sub:<parent>:<runid>:<stem>`
(two concurrent runs under one parent never collide on a same-stem agent file;
keeps the `sub:<parent>:` teardown prefix; `_short_subagent_id`'s
`rsplit(":", 1)[-1]` lands on the `agent-<id>` stem so the rendered header is
identical to an Agent/Task card). DISPLAY ONLY — these cards ride the existing
per-recipient `subagent_cards` gating + the W2 collapse-on-done, identically to
the Agent/Task shape (path 1 = the agent's own `text`+`end_turn`; path 2 = the
unchanged parent-finalize backstop). PLUS a THIRD, **deterministic close
collapse on the route FIFO** for the empty-final case (a Workflow agent ending
lifecycle-only never self-collapses and may have no later parent finalize):
the `<task-notification>` marks the bracket `closing` (not popped);
`check_sidechain_updates` tails the closing bracket's `wf_dir` ONE final time
(final display tail), THEN appends a `NewMessage(subagent_collapse_prefix=
"sub:<parent>:<runid>:")` AFTER the cards, THEN pops the bracket;
`bot.handle_new_message` routes that marker to
`message_queue.enqueue_subagent_collapse(route, prefix)` → a
`task_type="subagent_collapse"` route-FIFO control task that the per-route
worker runs AFTER the run's content tasks (the cards exist when it fires) →
the summary-gated `collapse_subagent_cards_with_prefix` (early-returns on
`keep`/verbose — the play-by-play stays live — and `off` has no slot). The
control task is ordered + retryable like content (`_RETRYABLE_TASK_TYPES =
{"content", "subagent_collapse"}` at the three `_run_with_retry` flood/retry
gates) so a flood-control window or a `RetryAfter` during the collapse's own
edit never silently drops it (the collapse is idempotent). Discovery is
bracket-gated (live only) and anchored (never `rglob`); restart degrades in
lockstep with run-state (in-memory brackets ⇒ no cards until a fresh launch
re-opens a bracket). Pull-only; no observer.

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

**Pane↔pane drift is a no-op (the di-copilot long-open-card churn fix — Fix A).**
The "next tick sees live `pane` == minted `pane` → no further re-render"
loop-safety above held ONLY for the `side_file`→pane flip, where both
fingerprints hash the SAME capture. For a pane↔pane comparison they do NOT: the
poller resolves `live` from a `scrollback=0` pane capture, while the card's pane
token was minted by `handle_interactive_ui` from a `scrollback=500` capture, and
the two `_pane_fingerprint`s differ PERMANENTLY for a busy/scrolled long-open AUQ
(the 500-line scrollback recovers options the 0-line visible pane lost). So a
`bail_aged` AUQ (side file aged past the 300s read-TTL → `kind=pane`) re-minted
EVERY ~1s tick forever — a per-tick in-place re-edit that periodically timed out
and recreated the card (the duplicate-card churn the owner saw in di-copilot).
Fix: `_remint_on_source_drift` now SHORT-CIRCUITS (returns False, no re-render)
when `minted[0] == "pane" and live.kind == "pane"` — a pane↔pane "drift" is just
capture noise, never a real source change (there is exactly ONE source when no
side file / `jsonl_cache` exists; the resolver itself documents the pane kind can
never legitimately `source_drift`). `_remint` stays armed for the genuine
`side_file`→pane / `jsonl_cache`→pane FLIP (`minted kind != "pane"`), so item-1
is untouched. RED-first: `test_same_hash_pane_to_pane_drift_does_not_remint`
(+ the existing `side_file`→pane drift tests stay green).

**Transient edit-outcome KEEPS the card (the churn's visible trigger — Fix B).**
The ~1Hz interactive re-edit (whether from the source-drift loop above or any
busy-topic re-render) periodically TIMES OUT against Telegram
(`telegram.error.TimedOut` → `_classify_bad_request` → `TopicSendOutcome.OTHER`).
`handle_interactive_ui`'s edit gate previously accepted only `OK` /
`MESSAGE_NOT_MODIFIED` and treated everything else as "edit failed → fresh send",
deleting the old card and sending a new one — a new message + notification PER
timeout (the user-visible spam; ~37 re-creates/hour on a 99-minute AUQ). Fix: a
transient `OTHER` / `RATE_LIMITED` edit outcome now KEEPS the existing card and
returns (the next poll re-edits in place); ONLY `MESSAGE_NOT_FOUND` (provably
gone) and the topic-broken outcomes (`TOPIC_NOT_FOUND` / `TOPIC_CLOSED` /
`FORBIDDEN`, which must reach the send-failed DM escalation) fall through to the
delete-old + send-new path. Mirrors the dashboard self-heal rule (`dashboard.py`
— never re-send on a transient, or the still-live message orphans; hermes Wave C
review P2-2). Behavior-narrowing (strictly FEWER sends) so it can never increase
Telegram traffic. **Residual (P3, visual-only):** the poller advances the
published render hash BEFORE the `handle_interactive_ui` edit (a concurrency
guard), so if a transient edit in the genuine *new-UI* branch is KEPT (not
recreated), that one render transition's visual update is dropped until the next
genuine UI change (the same-hash branch won't retry it). Never a wrong dispatch
(tokens / keyboard / pane-validated dispatch unaffected), and a strict
improvement over the recreate-churn it replaces. RED-first:
`TestInteractiveEditTransientOutcomeKeepsCard` (incl. the topic-broken
fall-through case).

**Render-only rescue resolver + render-identity loop kill (PR-3 PR-B — the busy
long-card render + duplicate-card loop).** A long-description AUQ in a BUSY topic
rendered BROKEN and SPAMMED duplicate "📋 details" cards every ~20s: the live tmux
pane mis-parsed / churned while the PreToolUse side file held the real question,
and the render path was gated behind a successful pane parse (so the side-file
rescue + the 📋 card were dropped exactly when needed), while the 1 Hz dedup hash
over the raw interactive-content excerpt CHURNED as scrollback scrolled under the
picker → a fresh re-render every tick. PR-A fixed the parser mis-parse; PR-B fixes
the render path + the loop. `auq_source.resolve_auq_source_for_render(window_id,
pane_text, explicit)` is the RENDER-path resolver (DISTINCT from the strict
`resolve_auq_source` that `validate_and_consume` + `_remint_on_source_drift` still
use UNCHANGED). It reads the side file READ-TTL-FREE then decides: **side_file_ok**
— side file consistent with the pane AND within the 300s read-TTL → render from it
+ mint TRUSTED tokens (the ONLY trusted side-file path; the `within_ttl` gate makes
it mirror the TTL'd strict resolver `validate_and_consume` re-resolves, so
mint/validate parity holds and a long-open card flips cleanly to `bail` at the TTL
boundary instead of stranding a trusted token the TTL'd validate rejects — no
dead-tap, and `_remint_on_source_drift` stays loop-safe because render's trusted
decision still agrees with the strict resolver it compares against); **bail** — the
pane is itself a COMPLETE coherent picker (`pane_form_is_complete_picker`) that
disagrees with the side file → a genuinely different / advanced live question →
render the PANE (trusted; never serve the stale side file); **rescue** — the pane
is unparseable / incomplete (busy scrollback) and the side file is the truth →
render the side file's full content DISPLAY-ONLY (`dispatch_trusted=False`, PURE
`build_form_from_tool_input` form — no pane overlay so the render identity can't
leak pane/scrollback churn); **explicit_jsonl / jsonl_cache / pane** — no side file
→ the pre-existing fallback (all trusted). `dispatch_trusted` GATES token minting
at the `_build_pick_button_rows` callsite: ANY untrusted render (rescue OR a
partial-pane bail) mints NO `pick_token` / `pick_intent` rows, calls
`prune_for_route` UNCONDITIONALLY — BEFORE the `p14_suppress_picks` skip, since an
untrusted partial bail is also p14 (hermes round-2: leaving a stale trusted token
row would make `_remint_on_source_drift` see minted≠live every tick → the very
re-render loop this PR kills; the trusted path self-prunes via `mint_row`'s
stale-row hygiene) — and adds a manual-nav notice (a busy/partial-pane digit can't
be verified against the live picker → would dead-tap). The ctx
(📋 full-descriptions) card is driven off the decision: side_file_ok / rescue post
the side file's descriptions (rescue is the V1/V2 fix — the card was previously
DROPPED because `resolve_record`'s pane-consistency check rejected on the busy pane);
**bail posts NO stale side-file card**. **Loop kill:** both `status_polling` dedup
hash sites (`_ui_render_hash`) hash the render IDENTITY for AskUserQuestion
(`auq_source.peek_render_identity` = the render decision + `render_signature` over
the render/keyboard-determining form fields — tabs, is_free_text, select_mode,
is_review_screen, options_complete, current_tab_inferred, len(questions),
`current_question_title`, and per-option number/label/cursor/selected/recommended)
instead of the raw interactive-content excerpt. `render_signature` uses
`current_question_title` ONLY — NEVER `pane_walkback_title` (scraped from the
churning scrollback above the option block; folding it in re-rendered the
title-less `bail`/`pane` card every tick, the dominant live single-select shape —
internal-review regression catch). This mirrors `_canonical_repr` and the OLD
`ui_content.content` hash, both of which excluded the title region above the
picker block, so the identity stays STABLE under scrollback churn (a rescue's
pure side-file form has no pane fields; a complete picker's parsed form ignores
scrollback above it) yet changes on every GENUINE transition (cursor move,
multi-select toggle, tab advance, review screen, complete↔incomplete,
JSONL-title, free-text, tab-inference loss). NEVER the cursor-blind pick-token
`fingerprint()` (the renderer paints the `❯` cursor + `selected` glyphs, so a
cursor/selection change MUST re-render — a separate render-only signature).
Non-AUQ interactive UIs (ExitPlanMode / permission) keep the raw-content hash.
**Disclosed residuals (all untrusted-display, never a wrong dispatch).** (1) The
≤1-poll-cycle boundary race at the 300s ageout (unchanged from item-1) — a
side_file_ok token minted just before the TTL and tapped just after it (before the
poller re-mints to `bail`/pane) routes through the existing source_drift
re-render and the 2nd tap dispatches; PR-B does not worsen it (it cleans the
>300s STEADY state, where render now picks `bail`→pane matching the strict
validate resolver). (2) A `rescue` renders the side-file question even if the side
file is STALE relative to a genuinely-different INCOMPLETE live pane (the OLD path
showed the partial live pane). Bounded — the PreToolUse hook overwrites the side
file on every AUQ, so the common sequential case stays fresh; staleness requires a
double-`--resume` sibling (session-keyed side file), a restart orphan, or a hook
write lag. dispatch_trusted=False (no buttons) so it is wrong-DISPLAY only, and it
is strictly better than the pre-PR-3 broken render (a raw scrollback blob); the
loop-kill FREEZES the rescue card so it self-corrects only when the side file is
overwritten / the pane becomes a complete picker. (3) A multi-question `rescue`
renders Q1 (`build_form_from_tool_input` defaults to the first question) even if
the live picker is on an advanced tab — only reachable when the pane is so
degraded its `←…→` tab header is unparseable (else PR-A → bail/side_file_ok with
the inferred tab); untrusted, and the 📋 ctx card still enumerates ALL questions.
Pull-only; no observer (c313657 forbidden).

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
(`md_capture.select_fresh_prose`), posts it as its own message, and records a
**shown-live marker** in the same per-session capture file. Idempotent via
`md_capture.was_shown_live` (consume-INCLUSIVE: a re-render / poll re-detect /
post-`kickstart` / the dedup having consumed the marker all skip a re-post). A
miss is a silent no-op — the JSONL copy delivers post-resolution exactly as
before (no marker, no dedup, never a delayed picker). A bounded ≤250ms retry
covers the rare same-tick race. Render-path state only — NOT a RouteRuntime
field (Bug-1 contract intact). The four `_maybe_post_live_prose` early returns
log a miss-classification line (`no_session` / `card_exists` / `capture_absent`
/ `not_before_reject` / `ttl_and_anchor_reject` / `empty_text` /
`already_shown_live`) so the next miss is diagnosable (PR-1 A6).

**Late-finalize stream-wait.** `_maybe_post_live_prose`'s base catch-up budget
is 250ms (`_LIVE_PROSE_RETRY_BUDGET_S`); the common clean case finalizes prose
BEFORE the picker is detected, so the first read hits. If the budget expires
with no finalized prose AND `md_capture.is_prose_streaming(session_id)` is True
(a message has deltas, no `final` yet, and its LATEST delta is within an 8s
recency window — the latest-delta anchor keeps a long stream live while a
crash-orphan ages out), the wait extends ONCE by
`_LIVE_PROSE_STREAM_WAIT_BUDGET_S` (3.0s) so a prose finalizing mid-stream still
posts BEFORE the card. A prose-less picker (no streaming) bails at the base
budget (zero added delay); a never-finalizing stream degrades to today's miss on
expiry (card created, JSONL delivers) — never hangs, never churns, pull-only.

**ExitPlanMode plan body BEFORE the card.** The EPM card carries no plan text
(only "Claude has written up a plan … proceed?" + options + a `ctrl+g … ·
~/.claude/plans/<slug>.md` footer), and the plan BODY is the tool's `input.plan`
— a synthetic `BLOCK_ORIGIN_EXIT_PLAN` text block buffered in JSONL until
resolution — so the user used to approve blind and get the plan AFTER. Fix:
`interactive_ui._maybe_post_epm_plan` (called from `handle_interactive_ui` AFTER
`_maybe_post_live_prose`, BEFORE the card, under the route lock → ordering
findings→plan→card) posts a "📋 Plan" message before the picker. The plan text
is `tool_input.plan` (replay) or, for a LIVE pane card (`tool_input` None), read
from the `~/.claude/plans/<slug>.md` file named in the pane footer
(`terminal_parser.extract_epm_plan_file_path`, footer-line-anchored; the read is
path-traversal-guarded to `~/.claude/plans/` + `asyncio.to_thread`). Idempotent
across poll re-renders + restart via an `md_capture` marker keyed by the plan's
`prose_norm_hash` (`record/was/read/consume_epm_plan_shown_live`, stored in the
same per-session NDJSON so `teardown_session` reclaims it). The post-resolution
JSONL copy is suppressed by a SECOND arm in
`session_monitor.filter_live_prose_duplicates` that aggregates the
`BLOCK_ORIGIN_EXIT_PLAN` block, hashes it via the SAME `prose_norm_hash` (the
plan-file text normalize-equals `input.plan` — mint/validate parity), and
matches the SEPARATE `epm_plan_shown_live` marker (never cross-matches real
prose; >1 group sharing a marker suppresses none). FAIL-OPEN: a hash mismatch /
missing file only fails to suppress (benign double-post) or skips the pre-post
(plan via JSONL) — never a wrong/lost post, never a crash. Pull-only; no
observer.

**Emission-anchor freshness — the additive-OR (PR-1, the dominant-miss fix).**
The original freshness was render-time `now` only: `now - final_at <= TTL`
(`AUQ_PROSE_TTL_S` 8s / `EPM_PROSE_TTL_S` 12s). The baked-in premise that "the
prose finalizes ~0.68s before the picker blocks" was INVERTED — measured (Wave-0
capture, Claude Code 2.1.172) the prose finalizes a gap BEFORE the picker is
DETECTED: ~5.44s idle, up to ~20.7s under bot load (the poller only scrapes on
its ~1s cadence and the adaptive watchdog can skip the blocked frame). So a fixed
render-time TTL routinely aged the matching prose out and the prose never posted.
`select_fresh_prose` now ORs the TTL leg with an **emission-anchor leg** keyed to
a STABLE picker-emission instant `emitted_at`: keep `r` iff
`(now - final_at <= ttl)  OR  (emitted_at is not None and  emitted_at -
emit_anchor_lookback_s <= final_at <= emitted_at + emit_anchor_eps_s)`, all still
AND-ed with the `not_before` turn boundary below. The OR can only WIDEN over the
TTL leg → provably non-regressive on the upper bound. The anchor SOURCE + its
eps/lookback constants are selected by modality in `_maybe_post_live_prose`:
**AUQ** → `auq_source.peek_side_file_written_at(session_id)` (the PreToolUse
side-file `written_at` ≈ the tool_use invocation; read-TTL-free, future-skew
guarded) with `_EMIT_ANCHOR_EPS_S` (2s) / `_EMIT_ANCHOR_LOOKBACK_S` (10s);
**ExitPlanMode** → `status_polling.peek_epm_surface_emitted_at(...)` (the poller's
FIRST-DETECTION stamp — EPM has no side file) with `_EMIT_ANCHOR_EPS_EPM_S` (2s)
/ `_EMIT_ANCHOR_LOOKBACK_EPM_S` (30s). The EPM lookback is LARGER because its
poller-stamp anchor lags the tool_use by the whole detect latency, whereas AUQ's
hook stamp sits ~at the tool_use; the AUQ lookback stays tight because it is ALSO
the restart-asymmetry guard — across a restart the on-disk AUQ `written_at`
survives (so `emitted_at` is non-None) while the in-memory `not_before` delivery
stamp is wiped to None, so the lookback is the ONLY floor left and must reject a
stale prior-turn prose finalized well before this picker's tool_use (EPM has no
on-disk anchor → `emitted_at` is None post-restart → the OR leg simply doesn't
fire, so its generous lookback is safe). The EPM stamp is poller-local
state: `status_polling._epm_surface_first_seen_at[route]`, `setdefault`-stamped
(first-detect, never a sliding window) wherever `ui_content.name ==
"ExitPlanMode"` is observed (the new-UI dispatch + the in-mode block), POPPED at
every EPM lifecycle end (the interactive-clear callback PRIMARY, the poller
mode-end / in-mode-absence / window-switch / window-gone seams, and
`clear_route_caches_for_topic`) so the NEXT EPM in the topic anchors to its OWN
instant; route-keyed so a double-`--resume` sibling never lights. Pull-only; no
observer.

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
in-memory stamp is gone → `not_before=None` disables THIS turn-boundary filter
(PR-1 NOTE: the AUQ emission-anchor `written_at` survives the restart, so its
lookback lower bound now carries the restart-asymmetry prior-turn guard — see the
additive-OR; the freshness falls to pure TTL-only only when `emitted_at` is ALSO
None, e.g. EPM or no side file — documented degradation, never a false-negative
on the live path); a rare **wall-clock-backwards** jump could mis-order a stamp vs a
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

## Cross-topic dashboard (Wave C)

One passive, owner+chat-scoped overview message per `(chat_id, owner_user_id)`,
owned by `handlers/dashboard.py` and persisted as the `dashboards` key in
`state.json` through SessionManager's single `_load_state`/`_save_state` path
(sync named mutators: `get/set/clear_dashboard`, `update_dashboard_msg_id`,
`set_dashboard_pinned`). `/dashboard` in any topic claims THAT topic as the
host (DM/General rejected; re-run elsewhere MOVES it, old message deleted
best-effort; `/dashboard pin` is the only pin path — never automatic, persisted
only on pin-API success). The whole Telegram-I/O-spanning claim/move/self-heal
flow serializes on a per-`(chat, owner)` `asyncio.Lock` with a post-send
loser-cleanup re-read (pre-C fix 1).

**Update driver is PULL-ONLY**: `maybe_refresh_dashboards` rides the existing
1s status-poll sweep (called once per sweep, not per binding — no observer,
c313657 forbidden). It renders the owner's view from
`session_manager.iter_thread_bindings()` + `route_runtime.snapshot(route)`,
**chat-scoped** (hermes review P1): `render_dashboard(owner_id, chat_id)`
includes only bindings whose persisted `group_chat_ids` mapping
(`session_manager.get_group_chat_id`) resolves to the dashboard's own chat —
FAIL CLOSED, an unresolvable chat is excluded from every dashboard, so a
dashboard in forum A never exposes forum B's topic names/states. That filter is
only as trustworthy as the mapping, so the **trust boundary** (hermes R2 P1):
`group_chat_ids` is written ONLY by the genuine bound-topic message seams
(`text/photo/voice/document_handler`, `forward_command_handler`,
`topic_edited_handler`) — `/dashboard` itself NEVER writes
`set_group_chat_id`, because thread ids are chat-local and a host claim in
chat B's unbound thread N would overwrite the mapping of chat A's bound topic
N and leak it onto chat B's dashboard. The dashboard instead carries its OWN
chat explicitly (the command's `effective_chat.id` at claim time, the
`dashboards` record key afterwards) through every
`topic_send`/`topic_edit`/`topic_delete` — those helpers take an explicit
`chat_id` and never resolve via `group_chat_ids`. It hashes the
rendered body and edits only on change — the hash covers state
lines, display names, and the binding set, so run-state transitions AND
bind/unbind/rename all repaint without a dedicated trigger; ages are
minute-coarse so the hash is stable within the minute (the implicit 60s age
tick). `MESSAGE_NOT_MODIFIED` is success (W8 precedent). Self-heal (re-send +
`update_dashboard_msg_id` under the lock) fires ONLY on `MESSAGE_NOT_FOUND` —
the distinctly-classified "message to edit not found" `BadRequest` in
`message_sender._classify_bad_request` — meaning the message is provably
deleted; a generic `OTHER` edit failure (timeout / unclassified transient)
logs and leaves the persisted msg_id + render hash alone so the next sweep
retries the edit (review P2-2 — re-sending on a transient would orphan the
still-live old message, unboundedly). The same rule applies to the same-topic
`/dashboard` rerun. A topic-shaped outcome
(`TOPIC_NOT_FOUND`/`TOPIC_CLOSED`/`FORBIDDEN`) clears the record — never a
self-heal loop into a dead topic — and the **chat-scoped** teardown seam
`dashboard.clear_dashboards_in_thread(thread_id, chat_id=…)` covers the host
topic closing: thread ids are chat-local (review P2-3), so only the
`(chat_id, thread_id)` records are cleared (`chat_id=None` — genuinely
unresolvable — falls back to the old all-chats sweep WITH a warning, never
stranding a record silently). Wired from `cleanup.clear_topic_state` (chat
resolved via `group_chat_ids`) AND from `bot.topic_closed_handler`'s
no-binding branch (review P2-4): a dedicated dashboard host topic has no
bound window, so without that branch its record would survive close until the
send-failure backstop (the host may have no bound window, so binding-centric
cleanup alone would miss it; pre-C fix 3).

**🔔 unanswered-turn derivation**: a route renders 🔔 when `run_state` is
`WAITING_ON_USER`, OR when it is idle and
`snapshot.last_assistant_turn_ended_at > snapshot.last_user_turn_at` — two
WALL-CLOCK stamps on the same `time.time()` clock. `last_user_turn_at` is
mirrored into route_runtime INSIDE `message_queue.set_route_user_turn_at`
(single writer ⇒ same-ts by construction) at the PRE-SEND delivery seams;
`last_assistant_turn_ended_at` is written only by the authoritative
end-of-turn branch from the event's JSONL timestamp, max-monotonic by event
time (out-of-order resume/rewind events never regress it; `None` timestamp
never updates). Either stamp `None` ⇒ never classified unanswered — the
documented **restart degradation**: the stamps are in-memory, so after a
restart the dashboard renders state-only until fresh turns repopulate them.
Boundary: `dashboard.py` sends via `message_sender` helpers only and never
touches message-queue internals or mutates route_runtime. Visibility is
honest: owner-filtered, NOT private — any forum member can read the message.

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
