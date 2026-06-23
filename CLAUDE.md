# CLAUDE.md

cc-telegram — Telegram bot that bridges Telegram Forum topics to Claude Code sessions via tmux windows. Each topic is bound to one tmux window running one Claude Code instance.

Tech stack: Python, python-telegram-bot, tmux, uv.

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/cctelegram/        # Type check — MUST be 0 errors before committing
uv run pytest -m scenario -q          # Scenario floor — black-box behavior tests at the public Telegram seam
bin/post-wave-check.sh                # Architecture deepening health diff (LoC, brittleness, tool status)
cc-telegram hook --install            # Auto-install the managed Claude Code hooks (SessionStart / PreToolUse / Notification)
```

## Core Design Constraints

- **1 Topic = 1 Window = 1 Session** — all internal routing keyed by tmux window ID (`@0`, `@12`), not window name. Window names kept as display names. Same directory can have multiple windows.
- **Topic-only** — no backward-compat for non-topic mode. No `active_sessions`, no `/list`, no General topic routing.
- **No message truncation** at parse layer — splitting only at send layer (`split_message`, 4096 char limit).
- **MarkdownV2 only** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text). Internal queue/UI code calls bot API directly with its own fallback.
- **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.
- **Rate limiting** — `AIORateLimiter(max_retries=5)` on the Application (30/s global). On restart, the global bucket is pre-filled to avoid burst against Telegram's server-side counter.
- **Scenario test floor** — `tests/scenarios/*.py` are black-box behavior tests at the public Telegram seam (`@pytest.mark.scenario`). They drive `Update` → real handler stack → fake tmux / fake bot, with no monkeypatch of handler internals in test bodies. Architecture changes must preserve these scenarios green.
- **RouteRuntime is the run-state authority** — `cctelegram.route_runtime` is the sole run-state / context-usage / idle-clear state machine (fed by `cctelegram.transcript_event_adapter`); it owns `RunState`, `ContextUsage`, and `IDLE_CLEAR_DELAY_SECONDS`. Mutations go through `ingest_transcript_event` / `mark_*`; reads come from `route_runtime.snapshot(route)`. Per-route `asyncio.Lock` only; no `register_state_callback` / `register_activity_callback` fan-out (that pattern produced bug c313657 and is precisely what `RouteRuntime` replaced). `message_queue` remains the only sender/editor of status cards; it queries `snapshot.status_card_visible` and writes back via `mark_status_card_published(route, msg_id)` — if it ever needs to mutate `message_queue` internals beyond that, the kill criterion fires (promote Route Outbox). A pane/lifecycle signal may **PROMOTE an active `RUNNING` route** (empty `open_tools`) to `WAITING_ON_USER` via `mark_interactive_pending` (retract via `mark_interactive_cleared`) for the window where Claude Code buffers the interactive `tool_use` (AskUserQuestion / ExitPlanMode) in JSONL — so the digest reads "🔔 Waiting on you" and typing stops while the prompt is live. Promotion fires only from a **pane-confirmed** live picker/plan-approval in `status_polling` (never the session-keyed side file alone — bit-neutral site (c) can't disambiguate a double-`--resume` sibling). The pane bit is strictly LOWER authority than the transcript (the deriver checks `open_tools` first; the `tool_use` / known-`tool_result` / end-of-turn / user branches zero it; plain-text/thinking and an unknown `tool_result` preserve it) and never resurrects idle, seeds an unseen route, overrides `RUNNING_TOOL`, or clobbers a transcript-set `WAITING_ON_USER`. It is cleared by the transcript reclaim (primary), the poller's mode-ended liveness reconciliation / in-mode tombstone, or route teardown (`clear_route`, now also called at the inbound stale-window unbind sites in `inbound_telegram.py`). The digest repaints on a run-state transition via the poller (pull-only; no observer — c313657 stays forbidden). Wave B adds a SECOND lower-authority derivation input, `notification_pending` (`mark_notification_pending` / `mark_notification_cleared`): set only by the poller from a window-predicated `Notification`-hook side-file read (`handlers/notify_source.py`), it outranks `RUNNING_TOOL` in the deriver (the Workflow approval case) but stays below a transcript-interactive open id; transcript clears are timestamp-qualified (`user` clears unconditionally; tool_result / end-of-turn / assistant events clear only when strictly NEWER than `notification_set_at`); the poller clears it when the pane is observed RUNNING at a capture strictly after `set_at + NOTIFY_PANE_CLEAR_MARGIN_S` (level + margin, NOT an idle→active edge — the adaptive capture can skip the blocked approval frame, so an edge requirement strands the bit; the blocked prompt replaces the run chrome, so a status-active frame after the hook fired is positive proof the user approved) and on the `NOTIFY_TTL_SECONDS` (30 min) runtime TTL; it may resurrect an IDLE(pane) route with a live `suspended_tools` stash (positive live proof). The two bits clear INDEPENDENTLY; `mark_notification_pending` returns a `NotificationMarkResult` that drives the poller's generation-guarded side-file unlink. GH #44 adds a THIRD lower-authority input, `background_agents` — a JSONL-derived per-route map (sidechain transcripts + parent async-launch / `<task-notification>` envelopes) applied as a **snapshot-time PROJECTION**: a stored-idle route with a live (non-expired, non-tombstoned) background key reports a visible RUNNING (typing + 🟡 Busy) while every mutator keeps byte-identical semantics on the STORED state; a committed `notification_pending` projects WAITING above the lift (🔔 outranks machine-busy — `mark_notification_pending` now COMMITS on stored-idle + live bg key, the second exception beside the stash resurrect). Keys: recorded by `mark_background_agent_activity` (idle path strictly ts-qualified `event_ts > last_assistant_turn_ended_at`, fail-closed on None; active/WAITING unconditional but foreground-presumed; also the ported Wave A heartbeat — pane-false-idle resurrection unqualified), upgraded by `mark_background_agent_launched` (the fixture-verified `agentId:` line in the async-launch tool_result; background keys are NEVER pruned), cleared by `mark_background_agent_done` (sidechain end-of-turn incl. lifecycle-only markers + parent task-notification task-id), the `BG_AGENT_TTL_SECONDS` (30 min) wall-clock heartbeat TTL (`_wall_now()`, expire-before-classify), the provenance-only foreground prune at the authoritative end-of-turn, and teardown. Done keys are TOMBSTONED; tombstones reset only on a GENUINE user turn — a task-notification user event (`TranscriptLifecycleEvent.is_task_notification`, adapter-stamped) preserves tombstones/pane-bit/stash, clears the notification bit ts-qualified only, and RE-DERIVES with preserved gates (never a forced RUNNING — `interactive_pending ⟺ pane-set WAITING` holds). Every key seam uses `utils.normalize_background_agent_key` (agentId == sidechain stem minus `agent-` == task-id). `pane_signals` stays decoration-only; the status CARD stays pane-driven (typing + digest/dashboard Busy are the projection surfaces — recorded product decision). Restart degradation: in-memory + stamp-None fail-closed (no lift until fresh parent activity). **Fix 5 (ISSUE-6 display cards, SHIPPED):** Workflow sub-agents ALSO surface as `↳` display cards — `check_sidechain_updates` enumerates the open bracket's `wf_dir.glob("agent-*.jsonl")` through `_track_and_emit_sidechain_file(..., feed_run_state=False)` (run-state isolation: `route_runtime` / `apply_sidechain_activity` UNCHANGED; the `wf-task:` bracket stays the SOLE Workflow run-state input), run-id-qualified key `sub:<parent>:<runid>:<stem>`. The cards ride the existing per-recipient `subagent_cards` gating + W2 collapse-on-done via THREE paths: (1) the agent's own `end_turn`+`text`, (2) the unchanged parent-finalize backstop, (3) a deterministic route-FIFO close collapse (the `<task-notification>` marks the bracket `closing`, the monitor tails the final tail + appends a `NewMessage(subagent_collapse_prefix)` → `enqueue_subagent_collapse` → a summary-gated `subagent_collapse` control task, flood/RetryAfter-safe via `_RETRYABLE_TASK_TYPES`). Pull-only; no observer.
- **Interactive-surface teardown is PARENT-only (sidechain-gated)** — both `bot.handle_new_message` seams that clear a live interactive card on the parent route — the explicit AUQ `tool_result` invalidation (`forget_ask_tool_input` + `auq_ledger.release_window`) and the generic "any non-interactive message ⇒ interaction complete" teardown (`if has_interactive_surface(...): clear_interactive_msg(...); forget_ask_tool_input(wid)`) — are gated on `msg.subagent_key is None`, mirroring the interactive-HANDLING branch and the sidechain-emit routing-bypass intent. A sidechain / background-agent block carries the PARENT's `session_id` + a non-None `subagent_key`, so it routes to the parent's route; without the gate a background Workflow/Agent narrating while the parent is BLOCKED on a live prompt tore the card down (`topic_delete`) and popped the by-window `_auq_context_posted` marker, so the poller re-detected the still-live pane prompt and re-posted (the 2026-06-23 DiCopilot ~28× ctx-card duplication; EPM `📋 Plan` re-post twin via `md_capture.teardown_session`). `has_interactive_surface` is route-keyed + UI-type-agnostic → one gate covers AUQ/EPM/Permission. A GENUINE parent block (`subagent_key is None`) still tears the card down. See `.claude/rules/message-handling.md` + `architecture.md`.

## Code Conventions

- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.

## Configuration

- Config directory: `~/.cc-telegram/` by default, override with `CC_TELEGRAM_DIR` env var.
- `.env` loading priority: local `.env` > config dir `.env`.
- State files:
  - `state.json` (thread bindings, window states, display names, read offsets)
  - `session_map.json` (hook-generated `window_id → session` mapping)
  - `monitor_state.json` (JSONL byte offsets per tracked session)
  - `interactive_state.json` (persisted picker msg ids + AUQ context markers; survives `launchctl kickstart`)
  - `auq_pending/<session_id>.json` (`PreToolUse` side files; one per active AUQ; mode `0600` under directory mode `0700`; survives multi-select `aqt:` toggles; cleaned when AUQ `tool_result` calls `forget_ask_tool_input`, on session replacement, or by startup GC)
  - `notify_pending/<session_id>.json` (Wave B `Notification` hook side files; window-keyed `{ts, window_key, generation, kind}` markers — NO notification message text; mode `0600` under dir `0700`. Read by `handlers/notify_source.py` with a HARD `window_key == tmux_session:window_id` predicate (double-`--resume` sibling safety) and consumed by the poller into `route_runtime.mark_notification_pending`; unlinked generation-guarded per the returned `NotificationMarkResult`, on session replacement / `/clear` / topic close, or by the 24h startup GC)
  - `auq_action_ledger.jsonl` (Wave 3 append-only write-ahead ledger of AUQ option-pick lifecycle states keyed by `(route_hash, fp8, opt)`; mode `0600`; latest line per key wins; the callback handler reads it BEFORE the in-memory `_pick_tokens` table so a duplicate tap after `launchctl kickstart` answers "Action already received" instead of re-dispatching to tmux. v2.1.168 states: `accepted → dispatched` (confirmed advance), or `not_advanced` (pre-commit bail, Enter never sent → callback falls through) / `commit_unconfirmed` (Enter sent, advance unconfirmed → refresh-only); `digit_sent`/`failed_*_digit` are legacy-only. `released` tombstones a window's rows on tool_result-confirmed resolution ONLY — `auq_ledger.release_window(window_id)` fires at the explicit AUQ `tool_result` branch in `bot.handle_new_message` AND at the startup reconciler's positive-proof branch (the bot-down-between-tool_result-and-the-live-seam crash window); NEVER at the generic `forget_ask_tool_input` teardown, whose other callers (`/clear` / session replacement / surface clear) are not resolution proof and would unmask a dispatched-but-UNRESOLVED row's single-use brake — so a same-day byte-identical AUQ reconstructing the same content-derived key is dispatchable again (`lookup` treats a latest `released` row as None). The 24h retention is enforced on READ: load collapses latest-per-key FIRST then drops an expired latest key (never resurrecting an older row); `lookup` re-checks the cutoff for a >24h process)
  - `pick_intent.jsonl` (D2 restart-recovery: durable per-callback-**token** AUQ pick mint-intent store; mode `0600`; append-only JSONL row + tombstone lines; 24h retention + compaction. Written at the fresh single-select / review-Submit `aqp:` render — NOT for `aqt:` toggles. After a restart wipes the in-memory pick tokens, the `peek_none` / `expired` callback branches read it to RECOVER + re-dispatch the first token-less tap on a still-open card via `pick_token.recover_and_consume`: **row-scoped single-use** (a row reservation + per-sibling action-ledger guard + a `consume_row` tomb), the full **owner + stale-window** auth pair, and **read-TTL-free** source parity (`auq_source.read_side_file_for_recovery`, comparing `_canonical_dict_fingerprint`). Recovery proceeds only on **positive proof of in-memory loss** (no `_pick_token_cache` row at the reconstructed key) so it is strictly the restart net, never double-handling the live path. Kept SEPARATE from `auq_action_ledger.jsonl` — that ledger stays the 24h durable single-use authority; writing recovery state into its latest-wins key would clobber a `dispatched` row. Tombed on AUQ/EPM resolution (`forget_ask_tool_input` → `teardown_window`), `/clear`, and topic close; orphan-safe via recovery-time form/source re-validation + the 24h GC. Render/callback state only — NOT a RouteRuntime field; pull-only)
  - `md_hook_settings.json` (Bug 2 bot-managed Claude Code settings registering the `MessageDisplay` hook; passed to bot-launched sessions via `claude --settings` so the live-prose hook is scoped to the bot's windows and is NOT written to global `~/.claude/settings.json`; re-written when its content drifts)
  - `msg_display/<session_id>.ndjson` (Bug 2 `MessageDisplay` live-prose capture; one per session keyed by the transcript filename stem — resume-safe; dir mode `0700`, files mode `0600`; the tiny stdlib appender hook appends each streaming `delta`, the bot accumulates them by `MessageDisplay.message_id` into completed prose, posts it before the picker card, and records shown-live/consumed dedup markers in the SAME file; removed on AUQ/EPM resolution (`forget_ask_tool_input`) / session replacement / `/clear` / topic close, 1h startup GC backstop)
  - `message_refs.db` (SQLite provenance index; path overridable via `CC_TELEGRAM_MESSAGE_REFS_DB_PATH`)
  - `log-archive/` (gzipped log rotations; only if the rotation LaunchAgent is installed)

## Hook Configuration

Auto-install: `cc-telegram hook --install`

Or manually in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "cc-telegram hook", "timeout": 5 }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion",
        "hooks": [{ "type": "command", "command": "cc-telegram hook", "timeout": 2 }]
      }
    ],
    "Notification": [
      {
        "hooks": [{ "type": "command", "command": "cc-telegram hook", "timeout": 2 }]
      }
    ]
  }
}
```

`SessionStart` writes `session_map.json` (window ↔ session resolution). `PreToolUse` (matcher `AskUserQuestion`) captures the structured `tool_input` to `~/.cc-telegram/auq_pending/<session_id>.json` so the bot can render each option's full description in the Telegram picker at first render. Multi-select AUQs use `aqt:` callbacks for non-ledgered bare-digit toggles; Tab reaches the review screen, where Submit/Cancel reuses the existing ledgered `aqp:` pick flow. `Notification` (matcher-less, Wave B) writes the window-keyed `notify_pending/<session_id>.json` marker when Claude blocks on a permission / approval prompt (incl. the Workflow tool's Bash-approval gate, which never reaches JSONL) so the poller can flip the route to "🔔 Waiting on you"; no notification text is stored. The bot logs a one-time startup warning if `PreToolUse` or `Notification` is missing; re-run `cc-telegram hook --install` to repair (installs all three).

**AUQ pick dispatch (v2.1.168)** — a single-select `aqp:` pick / review Submit/Cancel no longer trusts a bare digit (on v2.1.168 the notes-side-panel variant makes a digit only MOVE the cursor). `_dispatch_pick` arrow-NAVIGATES the live cursor to the tapped option, VERIFIES it landed (cursor-blind fingerprint + number + `_loose_label_match` + the review-Submit anchor), presses `Enter` (the version-stable commit), re-parses, and records the ledger `dispatched` lock ONLY after `_classify_advance` confirms the EXACT expected advance (`not_advanced` = pre-commit bail → fall through; `commit_unconfirmed` = Enter sent but unconfirmed → refresh-only). **Scoped to single-select `aqp:` + review Submit/Cancel; the multi-select `aqt:` toggle still dispatches a bare digit (filed fast-follow — AUQ is NOT globally fixed).**

A third hook event — `MessageDisplay` (Bug 2 live prose) — is **NOT** managed by `cc-telegram hook --install` and is **NOT** in `~/.claude/settings.json`. The bot writes its own `md_hook_settings.json` and passes it only to sessions it launches via `claude --settings <file>` (`tmux_manager._compose_launch_command`), scoping the hook to the bot's windows (it merges with the global `SessionStart`/`PreToolUse`). The hook command runs the tiny stdlib `_md_display_appender.py` directly (it must never import the package — the streaming-display path runs hooks with `forceSyncExecution`, so latency matters; `md_capture` benchmarks the appender against a bare interpreter start). The bot logs a one-time startup warning if it cannot write the settings file, and live prose silently falls back to post-resolution JSONL delivery.

## Documentation conventions

### README sync rule

Any change that adds **a hook** (SessionStart / PreToolUse / Stop / SubagentStop / etc.), **an env var** (`CC_TELEGRAM_*` or external config dependency), **a state file or directory** (under `~/.cc-telegram/` or `~/.claude/`), or **a new external config dependency** (launchd plist, log-rotate agent, etc.) MUST update `README.md` in the same PR — touching at minimum the relevant section among "What it does", "Configure", "Install the Claude Code hook", "State files", "Log rotation", or "Repository layout". Architecture-relevant changes must also update `.claude/rules/architecture.md`. Stale README is a P2 finding in `/codex` and `/hermes` review.

## Architecture Details

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/topic-architecture.md for topic→window→session mapping details.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
