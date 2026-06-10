# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - Topic-based routing: 1 topic = 1 window = 1 session             │
│  - /history: Paginated message history (default: latest page)      │
│  - /screenshot: Capture tmux pane as PNG                           │
│  - /esc: Send Escape to interrupt Claude                           │
│  - Send text → Claude Code via tmux keystrokes                     │
│  - Forward /commands to Claude Code                                │
│  - Create sessions via directory browser in unbound topics         │
│  - Tool use → tool result: edit message in-place                   │
│  - Interactive UI: AskUserQuestion / ExitPlanMode / Permission     │
│  - Per-user message queue + worker (merge, rate limit)             │
│  - MarkdownV2 output with auto fallback to plain text              │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │                                             │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive UIs (AskUserQuestion, ExitPlanMode, etc.)    │
│  - Parse status line (spinner + working text)                      │
└──────────┬──────────────────────────────────────────────────────────┘
           │                              │
           │ Notify (NewMessage callback) │ Send (tmux keys)
           │                              │
┌──────────┴──────────────┐    ┌──────────┴──────────────────────┐
│  SessionMonitor         │    │  TmuxManager (tmux_manager.py)  │
│  (session_monitor.py)   │    │  - list/find/create/kill windows│
│  - Poll JSONL every 2s  │    │  - send_keys to pane            │
│  - Detect mtime changes │    │  - capture_pane for screenshot  │
│  - Parse new lines      │    └──────────────┬─────────────────┘
│  - Track pending tools  │                   │
│    across poll cycles   │                   │
│  - Tail sidechains      │                   │
│    UNCONDITIONALLY;     │                   │
│    show_tool_calls only │                   │
│    gates display; per-  │                   │
│    tick parent activity │                   │
│    → mark_subagent_     │                   │
│    activity keep-alive  │                   │
└──────────┬──────────────┘                   │
           │                                  │
           ▼                                  ▼
┌────────────────────────┐         ┌─────────────────────────┐
│  TranscriptParser      │         │  Tmux Windows           │
│  (transcript_parser.py)│         │  - Claude Code process  │
│  - Parse JSONL entries │         │  - One window per       │
│  - Pair tool_use ↔     │         │    topic/session        │
│    tool_result         │         └────────────┬────────────┘
│  - Format expandable   │                      │
│    quotes for thinking │              SessionStart hook
│  - Extract history     │                      │
└────────────────────────┘                      ▼
                                    ┌────────────────────────┐
┌────────────────────────┐         │  Hook (hook.py)        │
│  SessionManager        │◄────────│  - Dispatch by         │
│  (session.py)          │  reads  │    hook_event_name:    │
│  - Window ↔ Session    │  map    │    SessionStart →      │
│    resolution          │         │      write session_map │
│  - Thread bindings     │         │    PreToolUse(AUQ) →   │
│    (topic → window)    │         │      write auq_pending │
│  - Message history     │────────►│      side file         │
│    retrieval           │  reads  │  - Receive hook stdin  │
└────────────────────────┘  JSONL  └────────────────────────┘

┌────────────────────────┐         ┌────────────────────────┐
│  MonitorState          │         │  Claude Sessions       │
│  (monitor_state.py)    │         │  ~/.claude/projects/   │
│  - Track byte offset   │         │  - sessions-index      │
│  - Prevent duplicates  │         │  - *.jsonl files       │
│    after restart       │         └────────────────────────┘
└────────────────────────┘

Additional modules:
  screenshot.py               ─ Terminal text → PNG rendering (ANSI color, font fallback)
  transcribe.py               ─ Voice-to-text transcription via OpenAI API (gpt-4o-transcribe)
  main.py                     ─ CLI entry point
  utils.py                    ─ Shared utilities (app_dir, atomic_write_json)
  route_runtime.py            ─ The sole per-route run-state / context-usage /
                                idle-clear authority. A lock-protected
                                RouteRuntimeSnapshot interface; owns RunState,
                                ContextUsage, IDLE_CLEAR_DELAY_SECONDS, and the
                                JSONL replay parser (parse_pending_tools_from_jsonl).
                                Also owns the lower-authority pane_interactive_pending
                                bit + mark_interactive_pending / mark_interactive_cleared
                                (PROMOTE an active RUNNING route → WAITING_ON_USER for a
                                buffered interactive tool_use; see the concurrency contract).
                                Busy-signal Wave A: records idle_source
                                ("transcript" = the authoritative end-of-turn branch;
                                "pane" = a pane clear that reconciled an ACTIVE route —
                                a pane clear on an already-idle route preserves the
                                value; lazy IDLE_RECENT→IDLE_CLEARED decay preserves it;
                                reset to None on leaving idle / mark_session_reset /
                                teardown) plus a suspended_tools stash: the pane-idle
                                reconciliation MOVES open_tools (ids + interactive
                                flags) into the stash instead of dropping them.
                                Restore paths: mark_subagent_activity resurrection, and
                                a transcript tool_result for a suspended id (checked
                                BEFORE the unknown-id branch — restores+closes via the
                                normal pairing). Drop paths: authoritative end-of-turn,
                                user lifecycle event, mark_inbound_sent,
                                mark_session_reset, route teardown. In-memory only
                                (restart recovery stays parse_pending_tools_from_jsonl
                                + seed_open_tools). mark_subagent_activity(route) is
                                the sidechain keep-alive mutator: on RUNNING /
                                RUNNING_TOOL it refreshes last_event_at + re-arms the
                                pane-idle debounce (no open_tools mutation); on idle
                                with idle_source=="pane" it RESURRECTS (restores the
                                stash → RUNNING_TOOL, or RUNNING on an empty stash;
                                clears idle deadlines); on transcript-idle / None it
                                no-ops; it never overrides WAITING_ON_USER (transcript-
                                or pane-bit-set) and never seeds an unseen route. Card
                                claim NARROWED: a status clear already enqueued before
                                resurrection MAY still delete the Busy card (no queue
                                generation-guard; no send-layer authority) — it
                                re-publishes on the next active status tick. Accepted
                                residual: a quiet sidechain (no writes) + blank pane is
                                uncovered; pane-spinner activity is the complementary
                                signal.
  transcript_event_adapter.py ─ Translates session_monitor.TranscriptEvent →
                                route_runtime.TranscriptLifecycleEvent and fans out
                                per-route. 150-250 LoC budget (kill signal at 250 —
                                beyond that it's Transcript Stream).
  md_capture.py               ─ Bug 2 MessageDisplay live-prose: bot-side reader.
                                Resolves the appender path + writes the bot-managed
                                `--settings` file (ensure_capture_settings), reads
                                the per-session NDJSON on demand and accumulates the
                                per-flush `delta`s into completed-prose ProseRecords
                                (read_prose_records — pull-only, no tailer/observer),
                                picks the fresh render candidate (select_fresh_prose,
                                TTL-gated + the Item-3/P2-1 STRICT `final_at >
                                not_before` turn-boundary filter; not_before is the
                                delivery wall-clock from message_queue, None ⇒ TTL-only),
                                owns the SINGLE dedup-parity hash
                                (normalize_prose / prose_norm_hash) shared with the
                                dedup, the shown-live marker store (record/read/
                                consume + the consume-inclusive was_shown_live idem-
                                potency guard — markers live in the same per-session
                                file), and the lifecycle (teardown_session / gc_stale —
                                gc_stale takes an INJECTED is_live_session predicate,
                                Item 3/P2-2: keep a live session's stale file + its
                                dedup markers, conservative-skip on predicate raise,
                                re-stat-before-unlink TOCTOU guard).
                                Imports utils only (the predicate is injected, never
                                imported — md_capture stays a leaf).
  _md_display_appender.py     ─ The MessageDisplay hook itself: a tiny stdlib-only
                                appender run directly by the interpreter (NEVER
                                imports the package — forceSyncExecution latency).
                                Keys the per-session file by Path(transcript_path).stem
                                (resume-safe), appends the raw payload as one NDJSON
                                line via a single O_APPEND os.write, always exits 0.

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + rate_limit_send
  message_queue.py    ─ Per-user queue + worker (merge, status dedup)
  status_polling.py   ─ Background status line polling (1s interval). Its
                        pane-absent AUQ-card clear gate consults
                        auq_source.side_file_live_for_window (the PreToolUse
                        side-file lifecycle authority) before tombstoning, so
                        an obscured pane (task-list overlay / scrolled Submit
                        screen) can't tear down a still-live question's card.
                        Also drives the pane-confirmed WAITING_ON_USER promotion
                        (mark_interactive_pending at SET sites a/b/d; site c is
                        bit-neutral), the mode-ended liveness reconciliation +
                        in-mode tombstone retract (mark_interactive_cleared), and
                        the per-tick digest repaint (_maybe_repaint_digest_on_transition
                        + the poller-local _prev_run_state dedup cache). Item 1:
                        its same-hash idle branch ALSO re-mints a live AUQ card
                        on SOURCE drift (side_file aged past the read-TTL → pane)
                        — re-resolve + resolve_ask_form (gates out non-AUQ panes) +
                        pick_token.peek_route_source by ROUTE (fingerprint-agnostic,
                        since the side-file-form and pane-form fingerprints differ)
                        vs the live source; on mismatch re-render via
                        handle_interactive_ui so the first tap dispatches (the
                        read-TTL itself is untouched). The D3-β sibling.
  response_builder.py ─ Response pagination and formatting
  interactive_ui.py   ─ AskUserQuestion / ExitPlanMode / Permission UI
  directory_browser.py─ Directory selection + session picker UI for new topics
  cleanup.py          ─ Topic state cleanup on close/delete
  callback_data.py    ─ Callback data constants
  auq_ledger.py       ─ Wave 3 restart-safe write-ahead ledger for AUQ
                        option-pick dispatches. JSONL at auq_action_ledger.jsonl
                        keyed by (route_hash, fp8, opt). v2.1.168 state machine:
                        accepted → dispatched (confirmed advance), or
                        not_advanced (pre-commit bail — Enter never sent) /
                        commit_unconfirmed (Enter sent, advance unconfirmed);
                        ``failed_reason`` carries the sub-reason. digit_sent /
                        failed_before_digit / failed_after_digit are legacy-only
                        (on-disk compat). ``released`` tombstones a window's rows
                        on tool_result-confirmed resolution ONLY:
                        ``release_window`` fires at the explicit AUQ
                        ``tool_result`` branch in ``bot.handle_new_message``
                        AND the startup reconciler's positive-proof branch —
                        NEVER at the generic ``forget_ask_tool_input`` teardown
                        (`/clear` / session replacement / surface clear are
                        not resolution proof; releasing there would remove a
                        dispatched-but-UNRESOLVED row's single-use brake) — so
                        a same-day byte-identical AUQ (same content-derived
                        key) is dispatchable again. 24h retention is enforced on READ —
                        load collapses latest-per-key FIRST then drops an
                        expired latest key (never resurrecting an older row);
                        ``lookup()`` re-checks the cutoff and treats a latest
                        ``released`` row as None. Otherwise ``lookup()`` returns
                        raw rows; the **callback handler** projects pre-restart
                        accepted rows to ``unknown`` (via
                        ``process_start_time()``) so it refreshes the card
                        instead of re-dispatching. ``pick_token``'s sibling-
                        claimed recovery guard filters by STATE: not_advanced /
                        released / failed_before_digit do NOT spend the row;
                        ``accepted`` stays claimed REGARDLESS of process epoch
                        (crash-ambiguous — Enter may have been sent).
  pick_intent.py      ─ D2 restart-recovery: durable per-callback-TOKEN AUQ pick
                        mint-intent store (leaf; imports only utils). Append-only
                        JSONL (row + tombstone lines) at pick_intent.jsonl, 24h
                        retention + compaction. record_row (fresh aqp: render,
                        supersede different-fp rows only) / lookup_intent
                        (validated, sibling-aware) / consume_row (row single-use)
                        / teardown_window / reset_for_tests. pick_token.
                        recover_and_consume reads it to re-dispatch a token-less
                        tap after a restart.

State files (~/.cc-telegram/ or $CC_TELEGRAM_DIR/):
  state.json               ─ thread bindings + window states + display names + read offsets
  session_map.json         ─ hook-generated window_id→session mapping (SessionStart)
  monitor_state.json       ─ poll progress (byte offset) per JSONL file
  interactive_state.json   ─ persisted picker msg ids + AUQ context markers
                             (survives launchctl kickstart)
  auq_pending/<sid>.json   ─ PreToolUse side files for AskUserQuestion;
                             captures tool_input before Claude renders picker;
                             dir mode 0700, files mode 0600; kept across
                             multi-select toggles; cleaned on AUQ tool_result,
                             session replacement, or startup GC
  auq_action_ledger.jsonl  ─ Wave 3 append-only ledger of AUQ option-pick
                             lifecycle transitions (mode 0600; latest line per
                             key wins). The callback handler consults this
                             BEFORE the in-memory token table so a duplicate
                             tap after process restart returns "Action already
                             received" instead of re-committing the pick. 24h
                             retention enforced on read (load + lookup; file
                             rewritten only by over-cap compaction). `released`
                             rows tomb a window's keys on tool_result-confirmed
                             AUQ resolution only (the AUQ tool_result branch in
                             bot.handle_new_message + the startup reconciler's
                             positive-proof branch — never the generic
                             forget_ask_tool_input teardown) so a re-asked
                             identical question is dispatchable again.
  pick_intent.jsonl        ─ D2 restart-recovery: durable per-callback-TOKEN AUQ
                             pick mint-intent store (mode 0600; append-only row +
                             tombstone JSONL; 24h retention + compaction). Written
                             at the fresh aqp: single-select/Submit render. After
                             a restart wipes the in-memory pick tokens, the
                             peek_none/expired branches RECOVER + re-dispatch the
                             first token-less tap (row-scoped single-use; owner +
                             stale-window auth; read-TTL-free source parity).
                             SEPARATE from auq_action_ledger.jsonl by design.
                             Tombed on AUQ/EPM resolution, /clear, topic close.
  md_hook_settings.json    ─ Bug 2 bot-managed Claude Code settings registering
                             the MessageDisplay hook; passed to bot-launched
                             sessions via `claude --settings` (NOT in global
                             ~/.claude/settings.json); merges with global hooks.
  msg_display/<sid>.ndjson ─ Bug 2 MessageDisplay live-prose capture; one per
                             session keyed by the transcript filename stem
                             (resume-safe); dir mode 0700, files mode 0600.
                             The appender appends each streaming delta; the bot
                             accumulates by MessageDisplay.message_id into
                             completed prose, posts it before the picker card,
                             and records shown-live/consumed dedup markers in the
                             SAME file. Removed on AUQ/EPM resolution
                             (forget_ask_tool_input) / session replacement /
                             /clear / topic close; 1h startup GC backstop.
  images/ + files/         ─ downloaded photo/document attachments forwarded to
                             Claude; dir mode 0700, downloads chmod'd 0600 after
                             write (uploads can carry sensitive content). The
                             dirs are create-and-REPAIRED to 0700 at import
                             (mkdir mode is a no-op on an existing dir, so an
                             upgraded install's 0755 is tightened); a chmod
                             OSError logs a WARNING and never fails the download.
  message_refs.db          ─ SQLite provenance index for reply-context resolution
  log-archive/             ─ gzipped rotations (only if rotation LaunchAgent installed)
```

## Key Design Decisions

- **Topic-centric** — Each Telegram topic binds to one tmux window. No centralized session list; topics *are* the session list.
- **Window ID-centric** — All internal state keyed by tmux window ID (e.g. `@0`, `@12`), not window names. Window IDs are guaranteed unique within a tmux server session. Window names are kept as display names via `window_display_names` map. Same directory can have multiple windows.
- **Hook-based session tracking** — Claude Code `SessionStart` hook writes `session_map.json`; monitor reads it each poll cycle to auto-detect session changes.
- **PreToolUse(AskUserQuestion) side files** — the `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured `tool_input` to `auq_pending/<session_id>.json` before Claude renders the picker. The bot reads the side file at picker render time so each option's full description is visible in the Telegram context message immediately, before terminal completion. Side files are mode 0600 under a 0700 directory; multi-select `aqt:` toggles keep them alive, and cleanup happens when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when the session is replaced, or via startup GC. Bot logs a one-time warning if `PreToolUse` is missing from `~/.claude/settings.json`; `cc-telegram hook --install` reinstalls both hooks.
- **TTL-free-but-pane-consistent dispatch source + live-safe side-file GC/reconcile (stateless-callback Wave 1 PR-B)** — three side-file-trust hardenings, all in `auq_source.py` + its startup wiring. (1) `resolve_auq_source_for_dispatch(window_id, pane_text) -> DispatchAuqSource(kind, payload, source_fingerprint, form)` is the read-TTL-FREE dispatch source: it reads the record via `_read_live_pretool_record(apply_ttl=False)` (the new `apply_ttl` keyword skips ONLY the `age > _PRETOOL_TTL_SECONDS` block — session-resolve, read, and the future-skew guard stay), KEEPS `_record_consistent_with_pane` (fail-closed → `pane` kind on inconsistency / obscured pane), and on a consistent side file returns `side_file` with the side-file form (`resolve_ask_form(record.tool_input, pane_text)`, which carries the question title the pane form lacks) + `source_fingerprint=_canonical_dict_fingerprint(record.tool_input)`. A long-open card thus never flaps `side_file`→`pane` purely on read-TTL ageout (the item-1 source-drift class); MUST NOT mutate `_pretool_ask_records` (`resolve_record` stays the sole mutator). ADDED + unit-tested only — NOT wired into the live `aqp:` dispatch (PR-C). (2) `gc_stale(*, is_live_session=None)` mirrors `md_capture.gc_stale`: after the age test and before the re-stat TOCTOU guard, an INJECTED predicate called with the file STEM (= `<session_id>`) → True skip-keep / Exception conservative-skip, so a live AUQ whose tool_use is buffered (stale-mtime side file but still the card's liveness authority) is not reaped at startup; wired at `bot.py` to `lambda sid: monitor.state.get_session(sid) is not None`. (3) `_hydrate_ask_tool_input_cache`'s startup reconciler now unlinks the side file only on POSITIVE resolution proof — it peeks the side file's captured `tool_use_id` (`auq_source.peek_side_file_tool_use_id`, a thin public accessor over `_read_pretool_side_file`) and unlinks ONLY if a matching AUQ `tool_result` exists in the JSONL tail (`SessionMonitor._auq_tool_result_present`, sharing the new `_read_jsonl_tail` helper with `_find_latest_pending_auq`); a still-BUFFERED tool_use (no tool_result) or an empty captured id → PRESERVE (closes the live-AUQ-side-file-deleted-on-startup latent bug). Session-keyed discipline preserved (peek + unlink the SAME `current_map` session).
- **MessageDisplay live-prose capture (Bug 2)** — assistant free-text prose written in the same turn as an `AskUserQuestion` / `ExitPlanMode` `tool_use` is co-flushed to the session JSONL only at resolution, so during a live prompt the prose is not on the bridge and the Telegram user would choose blind. Claude Code's `MessageDisplay` hook fires with each streaming `delta` BEFORE the picker blocks; a tiny stdlib appender (`_md_display_appender.py`, never imports the package — `forceSyncExecution` latency budget) writes each `delta` to `msg_display/<session>.ndjson` keyed by `Path(transcript_path).stem` (resume-safe: under `--resume` the JSONL is the original session's file the bot tracks, not the new hook-reported id). The hook is scoped to bot-launched sessions via a bot-managed `md_hook_settings.json` passed as `claude --settings` (merges with the global hooks; never in `~/.claude/settings.json`). The bot accumulates the per-flush deltas by `MessageDisplay.message_id` (no JSONL counterpart, so grouping is bot-side) into completed prose, read on demand at picker-render (`md_capture.read_prose_records` — pull-only, no tailer/observer; c313657 stays forbidden). `md_capture.normalize_prose` (via `prose_norm_hash`) is the SINGLE normalization used for both the live `norm_hash` and the post-resolution JSONL dedup, so the two compare equal (mint/validate parity). The §3.0 data-model prerequisite plumbs JSONL `message.id` + a `block_origin` marker (`BLOCK_ORIGIN_EXIT_PLAN`) through `ParsedEntry` / `TranscriptEvent` / `NewMessage` so dedup can group prose with its sibling interactive `tool_use` and exclude the synthetic ExitPlanMode plan text. **Live delivery (PR-C):** `interactive_ui.handle_interactive_ui` → `_maybe_post_live_prose` posts the freshest finalized capture (`select_fresh_prose`, TTL-gated + the Item-3/P2-1 turn-boundary filter) before the picker card, records a shown-live marker, and is idempotent via `was_shown_live` (consume-inclusive); a miss is a silent no-op (JSONL delivers post-resolution). **Turn-boundary filter (Item 3 / P2-1):** the per-session capture file holds a PRIOR turn's leftover prose until resolution-time teardown, so a still-within-TTL leftover could be posted above a picker whose own turn produced no prose. `select_fresh_prose(not_before=...)` adds a STRICT `final_at > not_before` gate where `not_before` is the wall-clock instant the bot DELIVERED the current user turn into tmux (`message_queue.set_route_user_turn_at`, stamped PRE-SEND at the `inbound_aggregator._send_bundle` + `bot.forward_command_handler` + `effort` callback delivery seams — the same `time.time()` clock as the appender's `captured_at`). `_maybe_post_live_prose` resolves the stamp INSIDE itself (`peek_route_user_turn_at`, not threaded through `handle_interactive_ui`'s 22 callers — auto-closes the on-pane + restart first-render holes); `not_before=None` ⇒ TTL-only (the restart degradation). **Dedup (PR-D):** `session_monitor.filter_live_prose_duplicates` runs on the poll batch before dispatch — groups by `(session_id, message.id)`, matches a group's REAL-text aggregate `norm_hash` to an unconsumed marker, suppresses + consumes (consume-once, restart-safe); >1 group sharing one marker → suppress none. **Teardown:** `teardown_session` wired at `forget_ask_tool_input` (primary, AUQ+EPM), the `/clear`/deleted-window seams in `session_monitor` (OLD session id), and `clear_topic_state`; 1h startup GC backstop. **Startup-GC liveness gate (Item 3 / P2-2):** `gc_stale(is_live_session=...)` skips reaping a live session's capture file (the dedup markers live in the same file — reaping a live picker's file would double-post at resolution). The predicate is INJECTED at the `bot.py` callsite (`monitor.state.get_session(sid) is not None`, keyed by the ndjson stem = original session id, covering AUQ+EPM); a predicate raise → conservative SKIP; a re-stat before `unlink` is the TOCTOU guard. Pull-only throughout (c313657 forbidden).
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via thread bindings (topic → window_id → session).
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. The pre-2026-02-11 `window_name`-keyed `state.json`/`session_map.json` format is no longer migrated: any non-`@` legacy keys found on load are dropped with a one-shot per-map `logger.warning` (`window_states` / `thread_bindings` / `user_window_offsets` in `session.py`; `session_map` entries in `session_monitor._load_current_session_map`). The live SessionStart hook only ever emits `@N` keys.
- **RouteRuntime concurrency contract** — `route_runtime` is the sole run-state / context-usage / idle-clear authority, exposing a single per-route state machine via `ingest_transcript_event(route, event)`, `mark_*(route)`, and `snapshot(route)`. Per-route `asyncio.Lock` serialises mutations within a route; independent routes do not serialise. Reads come only from `snapshot(route)` — each mutation freezes a committed, frozen `RouteRuntimeSnapshot` and there is no push/observer channel. Pane snapshots (`mark_pane_idle` / `commit_pane_idle_clear`) are reconciliation events with lower authority than transcript lifecycle: they preserve `WAITING_ON_USER`, only clear `RUNNING` / `RUNNING_TOOL`. Pane signals may also **PROMOTE an active `RUNNING` route** (empty `open_tools`) to `WAITING_ON_USER` via `mark_interactive_pending` — fired by `status_polling` from a **pane-confirmed** live AUQ picker / ExitPlanMode plan-approval while Claude Code buffers the interactive `tool_use` in JSONL — retracted via `mark_interactive_cleared`. Strictly lower authority than the transcript (deriver checks `open_tools` first; the `tool_use` / known-`tool_result` / end-of-turn / user branches zero the `pane_interactive_pending` bit, plain-text/thinking and an unknown `tool_result` preserve it); never resurrects idle, seeds an unseen route, overrides `RUNNING_TOOL`, or clobbers a transcript-set `WAITING_ON_USER`. Cleared by the transcript reclaim, the poller's mode-ended liveness reconciliation (`interactive_window != window_id`) / in-mode tombstone, or route teardown — dropped wherever route_runtime state is cleared: `mark_session_reset` (`/clear`), the `inbound_telegram` stale-window unbinds (direct `clear_route`), and `clear_topic_state` → `route_runtime.clear_routes_for_topic(user, thread)` on topic-close / poller window-gone (route_runtime's OWN topic-teardown seam — NOT derived from `message_queue._route_queues`, so a queue-less route is torn down too). The digest header repaints on a run-state transition via the poller (`_maybe_repaint_digest_on_transition` → `message_queue.refresh_activity_digest_if_present`; pull-only, no observer). No `register_*_callback` fan-out — that pattern (which produced bug c313657) is precisely what `RouteRuntime` replaced. Topic-broken handling is the **reactive** path in `message_queue` (`_bad_topic_threads` / `_emergency_dm` / `_TOPIC_BROKEN_OUTCOMES` / `probe_topic_liveness`), not a run-state — there is no `BROKEN_TOPIC` run-state.
- **Restart-safe AUQ pick dispatch (Wave 3 + v2.1.168 navigate-to-target)** — option-pick callback_data carries a stable `(route_hash, fp8, opt)` triplet in addition to the opaque token: `aqp:<route_hash>:<fp8>:<opt>:<token>`. The triplet is the key into `auq_action_ledger.jsonl` (append-only JSONL ledger). The callback handler consults the ledger BEFORE the in-memory `_pick_tokens` table, so a duplicate tap after `launchctl kickstart` answers "Action already received" instead of dispatching twice. Authorization remains the in-memory token + owner check — the ledger is for *idempotency*, not authentication. v4 §7.2 contract: owner-mismatch lookups peek the live token map and fall through to the token path only when the clicker holds a live token reconstructing the same key (legitimate collision); otherwise return `WRONG_USER_PICK_TEXT`. The keyed `aqp:<route_hash>:<fp8>:<opt>:<token>` shape is the only one the callback handler parses; the pre-Wave-3 `aqp:<token>` legacy shape is no longer accepted (a stray 1-part callback falls through to the malformed `else` → "Card expired, refreshing."). **The dispatch NAVIGATES the live cursor to the target option, VERIFIES, then presses Enter (v2.1.168 model — single-select `aqp:` + review Submit/Cancel ONLY).** On Claude Code v2.1.168 a richer "notes side-panel" picker variant makes a bare digit only MOVE the cursor (no select), so the bot can no longer trust a digit. `_dispatch_pick` (shared by the live `aqp:` path AND D2 recovery) finds the live `❯` cursor in `current_form`, computes `delta = target − cursor.number`, sends `Down`/`Up` × |delta| (`send_keys(enter=False, literal=False)`, return-checked), waits `NAV_SETTLE`, re-parses to VERIFY the cursor landed on the target (same cursor-blind fingerprint + `vc.number == target` + `_loose_label_match` + the review-Submit anchor for Submit), presses `Enter` (`enter=False, literal=False`), waits `COMMIT_SETTLE`, re-parses, and records `dispatched` ONLY after `_classify_advance` confirms the EXACT expected transition. Ledger non-success states: a **pre-commit bail** (cursor unknown / nav send False / verify fail — Enter provably never sent) records `not_advanced` and the callback **falls through** (a fresh-token re-tap re-validates); once `Enter` is sent an unconfirmed advance (incl. confirm capture/parse fail) records `commit_unconfirmed` and the callback **refreshes-only, never auto-redispatches**. The bare digit + the `auq_ledger.py` `digit_sent` / `failed_*_digit` states are now legacy-only (kept for on-disk compat). D2 restart-recovery inherits this automatically (it shares `_dispatch_pick`). **Scoped to single-select `aqp:` picks + review Submit/Cancel; the multi-select `aqt:` toggle still dispatches a bare digit (a filed fast-follow — AUQ is NOT globally fixed).** Validated against Claude Code v2.1.168 terminal behavior.
- **AUQ restart-recovery (D2)** — D3-β keeps a live card's *in-memory* pick tokens un-killable while the poller observes it, but a bot **restart** wipes them; the published card keeps its old keyboard with dead token strings, so the first tap hits `peek_none` and (pre-D2) degraded to the honest "tap again" modal for the card's whole life. D2 persists the per-token mint intent to a new leaf store (`pick_intent.py` → `pick_intent.jsonl`, written at the fresh `aqp:` single-select/Submit render; `aqt:` toggles excluded) so the `peek_none` / `expired` branches RECOVER and re-dispatch via `pick_token.recover_and_consume`. The store is keyed by the **token string** (a stale tap for form A can't read a newer same-key row B) and is kept **separate** from `auq_action_ledger.jsonl` — that ledger stays the 24h durable single-use authority; writing recovery state into its latest-wins `(route_hash, fp8, opt)` key would clobber a `dispatched` row and re-open double-dispatch. Recovery is **row-scoped**: a `_recovery_row_reservations[cache_key]` serialises concurrent sibling taps, a per-sibling action-ledger guard makes single-select single-use across siblings even across a crash, and a `consume_row` tomb is hygiene. It reproduces the live path's full **owner + `reject_stale_window_callback`** auth pair (the historic `peek_none` branch had neither) plus a callback-payload parity check against the stored intent, and **read-TTL-free** source parity (`auq_source.read_side_file_for_recovery`, comparing `_canonical_dict_fingerprint` — never the 12-hex `input_fingerprint`; pane fallback only when the side file is genuinely gone). The decisive invariant: recovery fires only on **positive proof of in-memory loss** (no `_pick_token_cache` row at the reconstructed `cache_key`) — a live row means the normal path owns it, a tombstoned row means this process just consumed it — so D2 is strictly the restart net and never double-handles the live path. The `accepted` claim is written INSIDE the row reservation (no release-then-claim gap), with a re-check of the cache-row + sibling proofs before it. Render/callback-path state only — NOT a `route_runtime` field; pull-only, no observer (c313657 stays forbidden). Tombed at `forget_ask_tool_input` (AUQ/EPM resolution + the `/clear` race via the OLD-window `forget_ask_tool_input(wid)` call) and `clear_topic_state`; orphan-safety is the recovery-time form/source re-validation + the 24h GC. Off-contract residual: a `jsonl_cache`-minted card DECLINES (its in-process getter is wiped on restart). The form fingerprint is now **cursor-blind on EVERY screen** — `AskUserQuestionForm._canonical_repr` omits the per-option cursor bit UNCONDITIONALLY (not just when `is_review_screen`); `auq_source._pane_fingerprint` shares that canonical so the pane source fingerprint collapses in lockstep. The cursor-blind fingerprint stays load-bearing under the v2.1.168 navigate-to-target dispatch: the bot MOVES the cursor to the target before committing, so the form identity must NOT change as the cursor moves (else the nav-verify re-parse would no longer match the minted fingerprint and every pick would bail `not_advanced`). A moved cursor — Submit↔Cancel on the review screen OR any option on a non-review picker — no longer rotates the pick token, so D2 recovery survives a cursor move on **every** screen; **the former D3-γ non-review DECLINE is RETIRED** (the non-review twin of the PR #28 review-screen fix). The review-Submit live + recovery guards share the cursor-blind `AskUserQuestionForm.review_submit_dispatchable` predicate (anchored on `is_review_screen` + option #1 + the literal `REVIEW_SUBMIT_LABEL` + the minted label; verified on Claude Code v2.1.161/.167/.168). The `_pane_fingerprint` ⇄ `_canonical_repr` shared-canonical coupling is load-bearing — guarded by the fingerprint-EQUALITY-across-cursor-move tests for BOTH the review screen and non-review pickers.
- **AUQ multi-select toggles** — multi-select option buttons use `aqt:<route_hash>:<fp8>:<opt>:<token>` and route to the interactive executor. `aqt:` validates the live token/window/form, dispatches a bare digit to tmux with no Enter, then re-renders from the pane. Toggles are not ledgered and do not consume sibling tokens; final Submit/Cancel is reached by Tab on the Claude Code review screen and reuses the existing `aqp:` pick/ledger flow. **The `aqt:` toggle still dispatches a bare digit** — under v2.1.168 the single-select `aqp:` pick/Submit moved to the navigate-to-target + Enter model (see the dispatch bullet above), but the multi-select toggle was left on the bare digit as a documented **fast-follow** (so AUQ is fixed for single-select picks + review Submit/Cancel, NOT globally). Converting `aqt:` to the navigate-to-target model is filed.
