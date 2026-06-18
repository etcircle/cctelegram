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
│    tick per-agent ticks │                   │
│    + launch/completion  │                   │
│    signals → keyed bg-  │                   │
│    agent marks (GH #44) │                   │
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
│    retrieval           │  reads  │    Notification →      │
└────────────────────────┘  JSONL  │      write notify_     │
                                   │      pending side file │
                                   │  - Receive hook stdin  │
                                   └────────────────────────┘

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
                                Restore paths: mark_background_agent_activity
                                resurrection (the keyed GH #44 successor of Wave A's
                                retired mark_subagent_activity), and
                                a transcript tool_result for a suspended id (checked
                                BEFORE the unknown-id branch — restores+closes via the
                                normal pairing). Drop paths: authoritative end-of-turn,
                                user lifecycle event (genuine only — a
                                task-notification user event PRESERVES the stash),
                                mark_inbound_sent,
                                mark_session_reset, route teardown. In-memory only
                                (restart recovery stays parse_pending_tools_from_jsonl
                                + seed_open_tools).
                                mark_background_agent_activity(route, key, ts) is
                                the keyed sidechain keep-alive mutator: on RUNNING /
                                RUNNING_TOOL it refreshes last_event_at + re-arms the
                                pane-idle debounce (no open_tools mutation); on idle
                                with idle_source=="pane" it RESURRECTS (restores the
                                stash → RUNNING_TOOL, or RUNNING on an empty stash;
                                clears idle deadlines — UNqualified, positive live
                                proof); on transcript-idle / None it leaves the
                                STORED state untouched (the GH #44 projection lifts
                                the visible state instead — see below); it never
                                overrides WAITING_ON_USER (transcript- or
                                pane-bit-set) and never seeds an unseen route. Card
                                claim NARROWED: a status clear already enqueued before
                                resurrection MAY still delete the Busy card (no queue
                                generation-guard; no send-layer authority) — it
                                re-publishes on the next active status tick. Accepted
                                residual: a quiet sidechain (no writes) + blank pane is
                                uncovered; pane-spinner activity is the complementary
                                signal.
                                Wave C dashboard turn stamps: two WALL-CLOCK snapshot
                                fields on the same time.time() clock as the delivery
                                stamps — last_user_turn_at (written ONLY by the sync
                                stamp_user_turn, mirrored from message_queue.
                                set_route_user_turn_at at the PRE-SEND delivery seams;
                                never mark_inbound_sent, which is post-send and loses
                                the fast-transcript race) and
                                last_assistant_turn_ended_at (written ONLY by the
                                authoritative end-of-turn branch from the EVENT's
                                JSONL timestamp, MAX-monotonic by event time —
                                out-of-order resume/rewind events never regress it;
                                None timestamp ⇒ no update, never ingest-time).
                                Cleared on mark_session_reset / clear_route /
                                clear_routes_for_topic; in-memory only (restart ⇒
                                dashboard renders state-only until repopulated).
                                last_event_at stays monotonic and is NEVER used for
                                the 🔔 unanswered-turn classification (ages only).
                                GH #44 background-agent projection: a THIRD
                                lower-authority input, background_agents
                                (normalized key → {last_seen_wall,
                                last_event_ts, is_background}) + done
                                tombstones, applied at SNAPSHOT time by the
                                single _build_snapshot/_projected_run_state
                                helper (every read path; no duplicate-freeze
                                drift): stored-idle + live key ⇒ visible
                                RUNNING (typing + 🟡 Busy); a committed
                                notification_pending projects WAITING above
                                the lift. Marks: mark_background_agent_
                                activity (keyed Wave A successor — heartbeat +
                                pane-false-idle resurrection unqualified; idle
                                key SET strictly ts-qualified vs
                                last_assistant_turn_ended_at, fail-closed),
                                mark_background_agent_launched (agentId: line
                                in the async-launch tool_result ⇒
                                is_background, never pruned),
                                seed_idle_and_mark_background_agent_launched
                                (PR-1 Half B: the launched mark but SEEDS an
                                IDLE_CLEARED+seen _RouteState if the route is
                                unseen, in one critical section — the bot
                                fan-out's launched-key handler so the restart
                                reconciler's relit wf-task: key lifts an
                                otherwise-stateless post-kickstart parent; a
                                no-op seed on an already-seeded route),
                                mark_background_agent_done (tombstones).
                                Clears: done / BG_AGENT_TTL_SECONDS 30-min
                                wall-clock heartbeat TTL (_wall_now(),
                                expire-before-classify) / provenance-only
                                foreground prune at end-of-turn / teardown.
                                mark_notification_pending commits on
                                stored-idle + live bg key (🔔 outranks the
                                lift). Task-notification user events
                                (is_task_notification, adapter-stamped)
                                preserve tombstones/pane-bit/stash, clear the
                                notification bit ts-qualified, and re-derive
                                with preserved gates. mark_subagent_activity
                                is RETIRED into the keyed mark. In-memory;
                                restart ⇒ stamp-None fail-closed (no lift
                                until fresh parent activity).
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
                                the PR-1 additive-OR of the render-time TTL leg with
                                an emission-anchor leg [emitted_at - lookback,
                                emitted_at + eps] — emitted_at is a stable
                                picker-emission instant: AUQ written_at / the EPM
                                poller stamp, selected by modality in interactive_ui;
                                recovers the dominant miss where the poller detected
                                the picker tens of seconds after the prose finalized,
                                blowing the TTL — + the Item-3/P2-1 STRICT `final_at >
                                not_before` turn-boundary filter; not_before is the
                                delivery wall-clock from message_queue, None ⇒
                                filter disabled [the anchor OR leg still applies];
                                only emitted_at=None ⇒ TTL-only),
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
  output_prefs.py     ─ Per-user output-verbosity resolution (plan v4 PR-1):
                        frozen OutputPrefs snapshot per recipient, layering
                        "stored user override > EXPLICITLY-set legacy env
                        default > preset" (env vars are defaults, never
                        ceilings). PRESETS verbose (≡ pre-settings behavior)
                        / standard (the production default since PR-2; the
                        TEST SUITE pins verbose via conftest so the floor
                        stays today-shaped) / compact / quiet. Stateless
                        leaf (imports config + session only); resolve(user_id)
                        is consulted at every emission point: the per-recipient
                        👤-echo gate in bot.handle_new_message (top of the
                        per-user loop, mirroring the removed monitor skip;
                        <task-notification> envelopes exempt via the public
                        response_builder.is_task_notification), the legacy
                        tool_activity gate at the old SHOW_TOOL_CALLS position
                        (drops ALL tool surfaces incl. Agent/Task — the
                        faithful env-false mapping; presets never set it),
                        digest line/snippet/live-line budgets in
                        _compact_*_line/_render_*_digest (live_lines=0 ⇒
                        header-only, NO hidden-events line), quiet's
                        digest_card=False (no digest state EVER created — incl.
                        _bump_agent_activity_counter, hermes r3 P1-1; images +
                        attention-dismiss still fire), subagent_cards=off (no
                        sidechain card; Wave A keep-alive unaffected),
                        agent_dispatch_msg=False (🤖 dispatch bubble suppressed
                        INSIDE _process_agent_task AFTER the _agent_tool_ids
                        stash, so the 🤖✅ report still renders — codex r2
                        P1-1), todo_card, context_footer, and /history's
                        user-echo filter (the ONLY pref history honors —
                        history stays the full-fidelity escape hatch). The
                        monitor-level user-entry skip + sidechain display drop
                        are REMOVED (session_monitor always emits;
                        consume_bot_sent_text stays in the monitor —
                        single-consumer). Stored per-user in state.json
                        "user_settings" via SessionManager named mutators
                        (downgrade loss accepted). UI: /settings command +
                        stg:<field>:<value>:<owner_user_id> callbacks in
                        callback_dispatcher/settings.py — owner check rejects
                        another allowed user's tap; preset tap = clean-slate
                        replace_user_settings. A STORED preset choice
                        overrides the ENTIRE env layer (env = defaults for
                        the un-chosen, never ceilings — hermes PR-1 P1).
                        PR-2 wires the collapse policies: W1
                        digest_on_done (keep / summary / delete) at
                        _finalize_activity_digest — summary = ONE-line
                        terminal render (run-state header survives, so a
                        post-turn 🔔 still shows; counts + duration frozen
                        on state at finalize for edit-stable repaints);
                        delete = the cancellation-safe removal protocol
                        (shield wraps the LOCK-HOLDING flush in both
                        debounce schedulers so cancel only lands in the
                        sleep; upsert re-checks tombstone + slot identity
                        under the lock; finalize-delete takes the lock,
                        tombstones, deletes best-effort, pops the slot —
                        restart-orphan accepted residual). W2
                        subagent_cards summary: the sidechain's own
                        end-of-turn (MessageTask.stop_reason, plumbed from
                        the NewMessage) collapses its ↳ card to one line
                        via the synchronous _collapse_subagent_digest;
                        _finalize_activity_digest is the backstop sweep for
                        empty-final sidechains; the collapsed slot is a
                        tombstone (late blocks never re-inflate; the 🤖✅
                        report is untouched). Fix 5 (ISSUE-6): the Workflow
                        shape rides this same contract PLUS a deterministic
                        route-FIFO close collapse —
                        enqueue_subagent_collapse puts a subagent_collapse
                        control task (flood/RetryAfter-safe via
                        _RETRYABLE_TASK_TYPES) that the per-route worker runs
                        AFTER the run's content tasks →
                        collapse_subagent_cards_with_prefix (summary-gated,
                        prefix-scoped, idempotent).
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
  notify_source.py    ─ Wave B Notification-hook side-file trust boundary
                        (leaf; imports session.peek + utils, tmux_manager
                        deferred). Owns notify_pending/<session_id>.json:
                        notification_pending_for_window applies the HARD
                        window_key == "tmux_session:window_id" read predicate
                        (double-resume sibling safety) + schema/future-skew
                        validation, deliberately NO read-TTL (staleness is
                        runtime-state-driven via NOTIFY_TTL_SECONDS in the
                        poller); unlink_if_generation_matches is the re-read
                        generation-guarded unlink (a hook re-fire between
                        read and unlink survives); unlink_for_session is the
                        teardown seam; gc_stale is the 24h startup backstop
                        with the injected is_live_session conservative-skip.
  dashboard.py        ─ Wave C cross-topic dashboard: one owner+chat-scoped
                        overview message per (chat_id, owner). Owns /dashboard
                        (claim the invoking topic as host; re-run elsewhere
                        MOVES it; /dashboard pin is opt-in), the pure renderer
                        (render_dashboard(owner_id, chat_id) — bindings filtered
                        to the owner AND to the dashboard's own chat via
                        session_manager.get_group_chat_id, FAIL CLOSED: an
                        unresolvable chat is excluded, never leaked cross-forum
                        (hermes review P1) + route_runtime.snapshot per route.
                        TRUST BOUNDARY (hermes R2 P1): /dashboard NEVER writes
                        set_group_chat_id — thread ids are chat-local, so a
                        host claim in chat B's unbound thread N would poison
                        the mapping of chat A's bound topic N and leak it onto
                        chat B's dashboard; group_chat_ids is written ONLY by
                        the genuine bound-topic message seams, and the
                        dashboard carries its OWN chat (effective_chat.id at
                        claim, the record key afterwards) explicitly through
                        every topic_send/topic_edit/topic_delete;
                        🔔 = WAITING_ON_USER or idle with
                        last_assistant_turn_ended_at > last_user_turn_at, both
                        non-None; ages minute-coarse from the monotonic
                        last_event_at), and the PULL-ONLY refresh driver
                        maybe_refresh_dashboards (called once per status-poll
                        sweep; rendered-content hash → edit only on change, so
                        run-state transitions AND bind/unbind/rename repaint
                        without an observer; MESSAGE_NOT_MODIFIED = success;
                        MESSAGE_NOT_FOUND — the distinctly-classified "message
                        to edit not found" — self-heals via re-send +
                        update_dashboard_msg_id; a generic OTHER edit failure
                        only logs and retries next sweep, NEVER re-sends
                        (re-sending on a transient would orphan the still-live
                        message — review P2-2); a topic-shaped outcome clears
                        the record — never a self-heal loop into a dead topic).
                        A per-(chat_id, owner_id) asyncio.Lock serializes the
                        whole Telegram-I/O-spanning claim/move/self-heal flow
                        (pre-C fix 1) with a post-send loser-cleanup re-read.
                        BOUNDARY: reads route_runtime.snapshot + session_manager,
                        sends via message_sender ONLY; never enqueues status
                        updates, never touches the message-queue module or its
                        send-layer caches, never mutates route_runtime, registers
                        no observer (c313657 forbidden). Persistence is
                        SessionManager-owned (state.json "dashboards" key, sync
                        get/set/clear/update_msg_id/set_pinned methods through
                        the ONE _load_state/_save_state path);
                        clear_dashboards_in_thread(thread_id, chat_id=…) is the
                        CHAT-SCOPED topic-teardown seam (thread ids are
                        chat-local — review P2-3; chat_id=None falls back to the
                        all-chats sweep with a warning), wired from
                        cleanup.clear_topic_state (chat resolved via
                        group_chat_ids) AND bot.topic_closed_handler's
                        no-binding branch so a dedicated binding-less dashboard
                        host topic is cleaned on close (review P2-4).
  pick_intent.py      ─ D2 restart-recovery: durable per-callback-TOKEN AUQ pick
                        mint-intent store (leaf; imports only utils). Append-only
                        JSONL (row + tombstone lines) at pick_intent.jsonl, 24h
                        retention + compaction. record_row (fresh aqp: render,
                        supersede different-fp rows only) / lookup_intent
                        (validated, sibling-aware) / consume_row (row single-use)
                        / teardown_window / reset_for_tests. pick_token.
                        recover_and_consume reads it to re-dispatch a token-less
                        tap after a restart.
  pane_signals.py     ─ GH #43 pane-derived per-route DECORATION store (true
                        leaf — imports nothing from the app; in-memory only).
                        Holds the latest pane-parsed background-shell count per
                        route (terminal_parser.parse_background_jobs: chrome-
                        region anchored — status-bar `· N shell` primary, churn
                        `· N shell(s) still running` fallback, MAX on conflict;
                        0 = chrome-present-no-token, None = no chrome → caller
                        skips so a bad frame never erases a fresh count).
                        Written by status_polling on every full capture
                        (record_background_jobs returns CHANGED → poller fires
                        refresh_activity_digest_if_present — pull-side repaint,
                        no observer); read by the collapsed done-card renderer
                        (`⏳ N background job(s)` suffix, IDLE routes only) and
                        /dashboard (⏳ replaces ⚪ on idle+fresh-count>0; 🔔
                        outranks). peek staleness BG_JOBS_MAX_AGE_S=30s (3× the
                        capture watchdog). NEVER a run_state input, NEVER
                        typing (recorded user decision). Teardown beside every
                        route_runtime clear seam: poller window-gone,
                        cleanup.clear_topic_state (topic-wide), inbound stale-
                        window unbinds, message_queue.teardown_route, bot
                        /clear + session_monitor rotation (mark_session_reset
                        sites).

State files (~/.cc-telegram/ or $CC_TELEGRAM_DIR/):
  state.json               ─ thread bindings + window states + display names +
                             read offsets + dashboards ("<chat_id>:<owner_id>" →
                             {thread_id, msg_id, pinned} — the /dashboard host
                             record; SessionManager-owned so the fixed-dict
                             state rewrite round-trips it) + user_settings
                             ("<user_id>" → {verbosity, knob overrides} — the
                             per-user /settings output-verbosity store;
                             shape-validated on load, knob values re-validated
                             by output_prefs on read; downgrade loss accepted)
  session_map.json         ─ hook-generated window_id→session mapping (SessionStart)
  monitor_state.json       ─ poll progress (byte offset) per JSONL file
  interactive_state.json   ─ persisted picker msg ids + AUQ context markers
                             (survives launchctl kickstart)
  auq_pending/<sid>.json   ─ PreToolUse side files for AskUserQuestion;
                             captures tool_input before Claude renders picker;
                             dir mode 0700, files mode 0600; kept across
                             multi-select toggles; cleaned on AUQ tool_result,
                             session replacement, or startup GC
  notify_pending/<sid>.json ─ Wave B Notification-hook side files; window-keyed
                             {ts, window_key, generation, kind} markers (mode
                             0600 under dir 0700) — NO notification message
                             text. Written by the hook on a Claude permission/
                             approval prompt; read by notify_source with the
                             hard window_key predicate; consumed by the poller
                             into route_runtime.mark_notification_pending and
                             unlinked generation-guarded per the returned
                             NotificationMarkResult; also unlinked on session
                             replacement, /clear, topic close; 24h startup GC.
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
- **PreToolUse(AskUserQuestion) side files** — the `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured `tool_input` to `auq_pending/<session_id>.json` before Claude renders the picker. The bot reads the side file at picker render time so each option's full description is visible in the Telegram context message immediately, before terminal completion. Side files are mode 0600 under a 0700 directory; multi-select `aqt:` toggles keep them alive, and cleanup happens when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when the session is replaced, or via startup GC. Bot logs a one-time warning if `PreToolUse` is missing from `~/.claude/settings.json`; `cc-telegram hook --install` reinstalls all three managed hooks (SessionStart / PreToolUse / Notification).
- **TTL-free-but-pane-consistent dispatch source + live-safe side-file GC/reconcile (stateless-callback Wave 1 PR-B)** — three side-file-trust hardenings, all in `auq_source.py` + its startup wiring. (1) `resolve_auq_source_for_dispatch(window_id, pane_text) -> DispatchAuqSource(kind, payload, source_fingerprint, form)` is the read-TTL-FREE dispatch source: it reads the record via `_read_live_pretool_record(apply_ttl=False)` (the new `apply_ttl` keyword skips ONLY the `age > _PRETOOL_TTL_SECONDS` block — session-resolve, read, and the future-skew guard stay), KEEPS `_record_consistent_with_pane` (fail-closed → `pane` kind on inconsistency / obscured pane), and on a consistent side file returns `side_file` with the side-file form (`resolve_ask_form(record.tool_input, pane_text)`, which carries the question title the pane form lacks) + `source_fingerprint=_canonical_dict_fingerprint(record.tool_input)`. A long-open card thus never flaps `side_file`→`pane` purely on read-TTL ageout (the item-1 source-drift class); MUST NOT mutate `_pretool_ask_records` (`resolve_record` stays the sole mutator). ADDED + unit-tested only — NOT wired into the live `aqp:` dispatch (PR-C). (2) `gc_stale(*, is_live_session=None)` mirrors `md_capture.gc_stale`: after the age test and before the re-stat TOCTOU guard, an INJECTED predicate called with the file STEM (= `<session_id>`) → True skip-keep / Exception conservative-skip, so a live AUQ whose tool_use is buffered (stale-mtime side file but still the card's liveness authority) is not reaped at startup; wired at `bot.py` to `lambda sid: monitor.state.get_session(sid) is not None`. (3) `_hydrate_ask_tool_input_cache`'s startup reconciler now unlinks the side file only on POSITIVE resolution proof — it peeks the side file's captured `tool_use_id` (`auq_source.peek_side_file_tool_use_id`, a thin public accessor over `_read_pretool_side_file`) and unlinks ONLY if a matching AUQ `tool_result` exists in the JSONL tail (`SessionMonitor._auq_tool_result_present`, sharing the new `_read_jsonl_tail` helper with `_find_latest_pending_auq`); a still-BUFFERED tool_use (no tool_result) or an empty captured id → PRESERVE (closes the live-AUQ-side-file-deleted-on-startup latent bug). Session-keyed discipline preserved (peek + unlink the SAME `current_map` session).
- **Render-only rescue resolver + render-identity loop kill (PR-3 PR-B)** — fixes a long-description AUQ in a BUSY topic rendering BROKEN + spamming duplicate "📋 details" cards every ~20s (the live pane mis-parses/churns while the PreToolUse side file holds the real question; PR-A fixed the parser mis-parse, PR-B fixes the render path + the loop). `auq_source.resolve_auq_source_for_render(window_id, pane_text, explicit) -> RenderAuqSource(decision, kind, payload, form, source_fingerprint, dispatch_trusted, reason)` is the RENDER-path resolver — DISTINCT from the strict `resolve_auq_source` that `pick_token.validate_and_consume` + `status_polling._remint_on_source_drift` still use UNCHANGED. It reads the side file READ-TTL-FREE then decides: `side_file_ok` (consistent with the pane AND within the 300s read-TTL → trusted; the `within_ttl` gate mirrors the TTL'd strict resolver validate re-resolves → mint/validate parity, so a long-open card flips cleanly to `bail` at the TTL boundary rather than stranding a trusted token validate rejects, and `_remint_on_source_drift` stays loop-safe), `bail` (the pane is itself a COMPLETE coherent picker — `pane_form_is_complete_picker` — disagreeing with the side file → a genuinely different/advanced live question → render the PANE, trusted; never serve the stale side file), `rescue` (unparseable/incomplete pane → render the side file DISPLAY-ONLY, `dispatch_trusted=False`, PURE `build_form_from_tool_input` form so the render identity can't leak pane churn), or the pre-existing `explicit_jsonl > jsonl_cache > pane` fallback when no side file. `dispatch_trusted` GATES token minting at the `_build_pick_button_rows` callsite (rescue → NO `pick_token`/`pick_intent` rows + `prune_for_route` + manual-nav notice); the ctx 📋 card is driven off the decision (side_file_ok/rescue post the side-file descriptions — rescue is the V1/V2 fix where the card was dropped because pane-consistency rejected on the busy pane; bail posts NO stale side-file card). **Loop kill:** both `status_polling` dedup hash sites (`_ui_render_hash`) hash the render IDENTITY for AskUserQuestion (`auq_source.peek_render_identity` = the decision + `render_signature` over the render/keyboard-determining form fields — using `current_question_title` ONLY, NEVER the scrollback-derived `pane_walkback_title`, which would churn the title-less bail/pane card every tick — internal-review regression catch; mirrors `_canonical_repr`), STABLE under scrollback churn yet re-rendering on every genuine transition; NEVER the cursor-blind pick-token `fingerprint()` (the renderer paints the cursor, so a cursor move must re-render); non-AUQ UIs keep the raw-content hash. MUST NOT mutate `_pretool_ask_records` (`resolve_record` stays the sole mutator). Disclosed residuals (all untrusted-display, never a wrong dispatch): the ≤1-poll-cycle 300s-boundary race (unchanged from item-1; PR-B cleans the >300s steady state); a `rescue` may render a STALE side-file question vs a different incomplete live pane (bounded — sibling/restart/hook-lag — and strictly better than the pre-PR-3 raw-blob render); a multi-Q `rescue` renders Q1 when the pane's tab header is unparseable (the 📋 card still enumerates all questions). Pull-only; no observer (c313657 forbidden).
- **MessageDisplay live-prose capture (Bug 2)** — assistant free-text prose written in the same turn as an `AskUserQuestion` / `ExitPlanMode` `tool_use` is co-flushed to the session JSONL only at resolution, so during a live prompt the prose is not on the bridge and the Telegram user would choose blind. Claude Code's `MessageDisplay` hook fires with each streaming `delta` BEFORE the picker blocks; a tiny stdlib appender (`_md_display_appender.py`, never imports the package — `forceSyncExecution` latency budget) writes each `delta` to `msg_display/<session>.ndjson` keyed by `Path(transcript_path).stem` (resume-safe: under `--resume` the JSONL is the original session's file the bot tracks, not the new hook-reported id). The hook is scoped to bot-launched sessions via a bot-managed `md_hook_settings.json` passed as `claude --settings` (merges with the global hooks; never in `~/.claude/settings.json`). The bot accumulates the per-flush deltas by `MessageDisplay.message_id` (no JSONL counterpart, so grouping is bot-side) into completed prose, read on demand at picker-render (`md_capture.read_prose_records` — pull-only, no tailer/observer; c313657 stays forbidden). `md_capture.normalize_prose` (via `prose_norm_hash`) is the SINGLE normalization used for both the live `norm_hash` and the post-resolution JSONL dedup, so the two compare equal (mint/validate parity). The §3.0 data-model prerequisite plumbs JSONL `message.id` + a `block_origin` marker (`BLOCK_ORIGIN_EXIT_PLAN`) through `ParsedEntry` / `TranscriptEvent` / `NewMessage` so dedup can group prose with its sibling interactive `tool_use` and exclude the synthetic ExitPlanMode plan text. **Live delivery (PR-C):** `interactive_ui.handle_interactive_ui` → `_maybe_post_live_prose` posts the freshest finalized capture (`select_fresh_prose`, the PR-1 additive-OR of the render-time TTL leg with an emission-anchor leg `[emitted_at - lookback, emitted_at + eps]` — `emitted_at` a stable picker-emission instant selected by modality: AUQ `auq_source.peek_side_file_written_at` / EPM `status_polling.peek_epm_surface_emitted_at`; recovers the dominant miss where the poller detected the picker tens of seconds after the prose finalized [measured 5.44s idle, ~20.7s loaded — the "~0.68s before the picker" premise was INVERTED], blowing the fixed TTL — + the Item-3/P2-1 turn-boundary filter) before the picker card, records a shown-live marker, and is idempotent via `was_shown_live` (consume-inclusive); a miss is a silent no-op (JSONL delivers post-resolution) logged with a miss-classification reason (PR-1 A6). **Turn-boundary filter (Item 3 / P2-1):** the per-session capture file holds a PRIOR turn's leftover prose until resolution-time teardown, so a still-within-TTL leftover could be posted above a picker whose own turn produced no prose. `select_fresh_prose(not_before=...)` adds a STRICT `final_at > not_before` gate where `not_before` is the wall-clock instant the bot DELIVERED the current user turn into tmux (`message_queue.set_route_user_turn_at`, stamped PRE-SEND at the `inbound_aggregator._send_bundle` + `bot.forward_command_handler` + `effort` callback delivery seams — the same `time.time()` clock as the appender's `captured_at`). `_maybe_post_live_prose` resolves the stamp INSIDE itself (`peek_route_user_turn_at`, not threaded through `handle_interactive_ui`'s 22 callers — auto-closes the on-pane + restart first-render holes); `not_before=None` disables the turn-boundary filter (the emission-anchor OR leg still applies when `emitted_at` is non-None; only `emitted_at=None` falls to TTL-only — the restart degradation). **Dedup (PR-D):** `session_monitor.filter_live_prose_duplicates` runs on the poll batch before dispatch — groups by `(session_id, message.id)`, matches a group's REAL-text aggregate `norm_hash` to an unconsumed marker, suppresses + consumes (consume-once, restart-safe); >1 group sharing one marker → suppress none. **Teardown:** `teardown_session` wired at `forget_ask_tool_input` (primary, AUQ+EPM), the `/clear`/deleted-window seams in `session_monitor` (OLD session id), and `clear_topic_state`; 1h startup GC backstop. **Startup-GC liveness gate (Item 3 / P2-2):** `gc_stale(is_live_session=...)` skips reaping a live session's capture file (the dedup markers live in the same file — reaping a live picker's file would double-post at resolution). The predicate is INJECTED at the `bot.py` callsite (`monitor.state.get_session(sid) is not None`, keyed by the ndjson stem = original session id, covering AUQ+EPM); a predicate raise → conservative SKIP; a re-stat before `unlink` is the TOCTOU guard. Pull-only throughout (c313657 forbidden).
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via thread bindings (topic → window_id → session).
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. The pre-2026-02-11 `window_name`-keyed `state.json`/`session_map.json` format is no longer migrated: any non-`@` legacy keys found on load are dropped with a one-shot per-map `logger.warning` (`window_states` / `thread_bindings` / `user_window_offsets` in `session.py`; `session_map` entries in `session_monitor._load_current_session_map`). The live SessionStart hook only ever emits `@N` keys.
- **RouteRuntime concurrency contract** — `route_runtime` is the sole run-state / context-usage / idle-clear authority, exposing a single per-route state machine via `ingest_transcript_event(route, event)`, `mark_*(route)`, and `snapshot(route)`. Per-route `asyncio.Lock` serialises mutations within a route; independent routes do not serialise. Reads come only from `snapshot(route)` — each mutation freezes a committed, frozen `RouteRuntimeSnapshot` and there is no push/observer channel. Pane snapshots (`mark_pane_idle` / `commit_pane_idle_clear`) are reconciliation events with lower authority than transcript lifecycle: they preserve `WAITING_ON_USER`, only clear `RUNNING` / `RUNNING_TOOL`. Pane signals may also **PROMOTE an active `RUNNING` route** (empty `open_tools`) to `WAITING_ON_USER` via `mark_interactive_pending` — fired by `status_polling` from a **pane-confirmed** live AUQ picker / ExitPlanMode plan-approval while Claude Code buffers the interactive `tool_use` in JSONL — retracted via `mark_interactive_cleared`. Strictly lower authority than the transcript (deriver checks `open_tools` first; the `tool_use` / known-`tool_result` / end-of-turn / user branches zero the `pane_interactive_pending` bit, plain-text/thinking and an unknown `tool_result` preserve it); never resurrects idle, seeds an unseen route, overrides `RUNNING_TOOL`, or clobbers a transcript-set `WAITING_ON_USER`. Cleared by the transcript reclaim, the poller's mode-ended liveness reconciliation (`interactive_window != window_id`) / in-mode tombstone, or route teardown — dropped wherever route_runtime state is cleared: `mark_session_reset` (`/clear`), the `inbound_telegram` stale-window unbinds (direct `clear_route`), and `clear_topic_state` → `route_runtime.clear_routes_for_topic(user, thread)` on topic-close / poller window-gone (route_runtime's OWN topic-teardown seam — NOT derived from `message_queue._route_queues`, so a queue-less route is torn down too). The digest header repaints on a run-state transition via the poller (`_maybe_repaint_digest_on_transition` → `message_queue.refresh_activity_digest_if_present`; pull-only, no observer). No `register_*_callback` fan-out — that pattern (which produced bug c313657) is precisely what `RouteRuntime` replaced. Topic-broken handling is the **reactive** path in `message_queue` (`_bad_topic_threads` / `_emergency_dm` / `_TOPIC_BROKEN_OUTCOMES` / `probe_topic_liveness`), not a run-state — there is no `BROKEN_TOPIC` run-state.
- **Notification-hook `notification_pending` bit (Wave B busy-signal)** — the SECOND lower-authority derivation input in `route_runtime`, for the previously invisible Workflow/permission approval waits (the gate blocks Claude with its `tool_use` open and NO JSONL trace, so the topic showed "🟡 Busy" forever). The Claude Code `Notification` hook (matcher-less, managed by `cc-telegram hook --install`; one-time startup warning when missing) writes `notify_pending/<session_id>.json` — `{ts, window_key, generation, kind}`, NO message text — and `handlers/notify_source.py` is its trust boundary: reads are HARD-predicated on `window_key == "tmux_session:window_id"` (a double-`--resume` sibling never lights), schema/future-skew validated, deliberately read-TTL-free. The poller consumes it at the TOP of the per-binding path (BEFORE the transition repaint and the adaptive capture gating, so a capture-skipped tick still consumes and a 🔔 transition repaints the digest the SAME tick) via `mark_notification_pending(route, set_at, generation)`, whose returned `NotificationMarkResult` DRIVES the generation-guarded unlink: `committed-live` → unlink AFTER the commit; `redundant-transcript-waiting` (already a transcript-set WAITING) / `stale-unlinked` (idle(transcript) or idle(pane) with an EMPTY stash) → unlink; `ignored-no-unlink` (unseen route — never seed). **Deriver precedence (top wins):** (1) transcript-interactive open id → WAITING_ON_USER; (2) `notification_pending` (over ANY `open_tools`, incl. a non-interactive Workflow id, or empty) → WAITING_ON_USER; (3) `pane_interactive_pending` with empty `open_tools` → WAITING_ON_USER; (4) non-interactive open tools → RUNNING_TOOL; (5) empty+active → RUNNING. The two bits clear INDEPENDENTLY; the pane bit's contract is untouched. **The IDLE(pane)+stash exception:** a notification on an idle route is stale by definition — EXCEPT idle(pane) with a non-empty `suspended_tools` stash, which is positive live proof the pane clear was false: the mark RESTORES the stash into `open_tools` and derives WAITING (the second stash-restore path beside Wave A's sidechain resurrection). **CLEAR rules:** a transcript `user` event clears unconditionally; `tool_result` / authoritative end-of-turn / assistant `tool_use` / `<task-notification>` clear ONLY when the event's JSONL timestamp (plumbed as `TranscriptLifecycleEvent.timestamp` by the adapter; parse failure ⇒ None) is strictly NEWER than `notification_set_at` — None/older PRESERVES (buffered pre-notification JSONL must not re-hide the wait; a preserved bit at end-of-turn keeps the route WAITING instead of idling); an unknown `tool_result` preserves (mirror of the pane bit). **Fix 1 (ISSUE-5 arm A): plain assistant `text`/`thinking` narration NO LONGER clears the bit** — a Workflow narrates *while* blocked, so the narration branches call `_clear_notification_if_setat_invalid` (the corrupt `set_at=None` invariant repair ONLY, reason INVARIANT), never the causal `_maybe_clear_notification_by_ts`. The poller clears when the pane is observed RUNNING at a capture taken strictly after `set_at + NOTIFY_PANE_CLEAR_MARGIN_S` (the user acted in the terminal — LEVEL + margin, NOT an idle→active edge: the adaptive watchdog capture can skip the blocked approval frame entirely, so an edge requirement strands the bit when the last pre-notification capture was already running; the blocked prompt REPLACES the run chrome, so a status-active frame sufficiently after the hook fired is positive proof execution resumed, and the margin keeps a same-tick capture of the pre-prompt frame from clearing early) and enforces `NOTIFY_TTL_SECONDS` (1800s — a product value: prompts are normally acted on within a session; past it the 🔔 silently degrades to 🟡 and the prompt stays discoverable on the pane) from RUNTIME state every tick, independent of side-file existence (a consumed file or a None-timestamp stream can never strand 🔔); pending-without-set_at violates the invariant and is treated as expired. Teardown drops the bit wherever route state clears (`mark_session_reset`, `clear_route`, `clear_routes_for_topic`); the side file is also unlinked on session replacement / `/clear` (old session id) / topic close, with the 24h `notify_source.gc_stale` (injected `is_live_session` conservative-skip) as the startup backstop. Pull-only throughout; no observer (c313657 stays forbidden).
- **Busy-signal completeness (ISSUE-5 + ISSUE-6) — full contract in `message-handling.md`.** Two coupled gaps closed in one wave (Fixes 1–4); the `↳` sub-agent DISPLAY cards for Workflow sidechains shipped as Fix 5 (see below). **Fix 1 (ISSUE-5 arm A):** plain assistant `text`/`thinking` narration no longer causally clears `notification_pending` (a Workflow narrates *while* blocked) — the narration branches call `_clear_notification_if_setat_invalid` (invariant repair only). **Fix 2 (ISSUE-6 + ISSUE-5 arm B):** the `Workflow` tool's background subagents now light typing + 🟡 via a parent-transcript bracket keyed `wf-task:<task_id>` that reuses the GH #44 `background_agents` marks verbatim (identity through `normalize_background_agent_key`). The launch anchor is STRUCTURED-primary (PR-2): `response_builder.workflow_launch_info_from_meta` reads the ENTRY-level `toolUseResult` (`{status:"async_launched", taskId, runId, transcriptDir}`, plumbed onto the tool_result `ParsedEntry.tool_result_meta` by `transcript_parser`; keyed on `taskId`, NEVER `status` alone — the Agent/Task `agentId` async-launch shares `status` but has no `taskId`), with `response_builder.extract_workflow_launch_info` (Task ID is MID-LINE — `(?im)^.*\bTask ID:…` — the captured id == the `<task-notification>` close key) as the PROSE FALLBACK (WARNING-logged for drift detectability); `transcriptDir` IS the validated `wf_dir` (no run-id-topology/glob). `session_monitor` opens a persistent `_WorkflowBracket`, emits a `bracket_heartbeats` refresh ONLY on a `wf_dir` `*.jsonl` mtime ADVANCE (DESIGN B — a separate channel from `ticks`; no parsing of sidechain entries for run-state), ages out via `BG_AGENT_TTL_SECONDS` when writes stop, and closes GATE-ON-BRACKET (the `<task-notification>` emits the `wf-task:` done key IFF a live bracket exists — no id-format guessing). The live `wf-task:` key is also what makes ISSUE-5 arm B re-light (§3.6) instead of STALE_UNLINK. **Fix 3 (ISSUE-5 durable surface):** a typed `NotificationClearReason` channel (`notification_clear_reason` snapshot field; every True→False stamps a reason) drives a persistent, audible `attention.notify_waiting(kind="notification_decision")` decision card posted by the poller on `COMMITTED_LIVE` (gated by `has_interactive_surface`), kept via `_reconcile_decision_card` (retry-while-pending; the END_OF_TURN+live-bg-key EOT-gap keep; a `DECISION_CARD_EOT_GRACE_S` grace for the monitor's EOT-before-launch-fanout race), and dismissed kind-aware (`attention.dismiss_if_kind` — all generic display-layer `attention.dismiss` sites converted to `kind="interactive_ui"` so they never ack the decision card). Pull-only throughout; no observer. **Fix 5 (ISSUE-6 owner decision #2 — SHIPPED): Workflow `↳` DISPLAY cards.** `check_sidechain_updates` adds a SECOND, anchored `bracket.wf_dir.glob("agent-*.jsonl")` enumeration over the parent's OPEN brackets (the SAME `wf_dir` the heartbeat stats), driven through `_track_and_emit_sidechain_file(..., feed_run_state=False)` so Workflow sidechain ENTRIES NEVER feed run-state (the `wf-task:` bracket + mtime heartbeat stay the SOLE Workflow run-state input — `route_runtime` / `apply_sidechain_activity` / `_finalize_activity_digest` UNCHANGED). Run-id-qualified key `sub:<parent>:<runid>:<stem>` (concurrent-run disjoint; keeps the `sub:<parent>:` teardown prefix; `_short_subagent_id` renders the `agent-<id>` stem only). DISPLAY ONLY — rides the existing per-recipient `subagent_cards` gating + W2 collapse-on-done (path 1 own end-of-turn / path 2 parent backstop) PLUS a THIRD deterministic **route-FIFO close collapse**: the `<task-notification>` marks the bracket `closing` (not popped); `check_sidechain_updates` tails the final tail, appends a `NewMessage(subagent_collapse_prefix)`, pops the bracket; `bot.handle_new_message` → `message_queue.enqueue_subagent_collapse(route, prefix)` → a `subagent_collapse` route-FIFO control task (flood/RetryAfter-safe via `_RETRYABLE_TASK_TYPES`) → summary-gated `collapse_subagent_cards_with_prefix` (keep/verbose stays live). Bracket-gated + anchored discovery (never `rglob`); restart-degrades in lockstep with run-state. Pull-only; no observer.
- **Restart-safe AUQ pick dispatch (Wave 3 + v2.1.168 navigate-to-target)** — option-pick callback_data carries a stable `(route_hash, fp8, opt)` triplet in addition to the opaque token: `aqp:<route_hash>:<fp8>:<opt>:<token>`. The triplet is the key into `auq_action_ledger.jsonl` (append-only JSONL ledger). The callback handler consults the ledger BEFORE the in-memory `_pick_tokens` table, so a duplicate tap after `launchctl kickstart` answers "Action already received" instead of dispatching twice. Authorization remains the in-memory token + owner check — the ledger is for *idempotency*, not authentication. v4 §7.2 contract: owner-mismatch lookups peek the live token map and fall through to the token path only when the clicker holds a live token reconstructing the same key (legitimate collision); otherwise return `WRONG_USER_PICK_TEXT`. The keyed `aqp:<route_hash>:<fp8>:<opt>:<token>` shape is the only one the callback handler parses; the pre-Wave-3 `aqp:<token>` legacy shape is no longer accepted (a stray 1-part callback falls through to the malformed `else` → "Card expired, refreshing."). **The dispatch NAVIGATES the live cursor to the target option, VERIFIES, then presses Enter (v2.1.168 model — single-select `aqp:` + review Submit/Cancel ONLY).** On Claude Code v2.1.168 a richer "notes side-panel" picker variant makes a bare digit only MOVE the cursor (no select), so the bot can no longer trust a digit. `_dispatch_pick` (shared by the live `aqp:` path AND D2 recovery) finds the live `❯` cursor in `current_form`, computes `delta = target − cursor.number`, sends `Down`/`Up` × |delta| (`send_keys(enter=False, literal=False)`, return-checked), waits `NAV_SETTLE`, re-parses to VERIFY the cursor landed on the target (same cursor-blind fingerprint + `vc.number == target` + `_loose_label_match` + the review-Submit anchor for Submit), presses `Enter` (`enter=False, literal=False`), waits `COMMIT_SETTLE`, re-parses, and records `dispatched` ONLY after `_classify_advance` confirms the EXACT expected transition. Ledger non-success states: a **pre-commit bail** (cursor unknown / nav send False / verify fail — Enter provably never sent) records `not_advanced` and the callback **falls through** (a fresh-token re-tap re-validates); once `Enter` is sent an unconfirmed advance (incl. confirm capture/parse fail) records `commit_unconfirmed` and the callback **refreshes-only, never auto-redispatches**. The bare digit + the `auq_ledger.py` `digit_sent` / `failed_*_digit` states are now legacy-only (kept for on-disk compat). D2 restart-recovery inherits this automatically (it shares `_dispatch_pick`). **Scoped to single-select `aqp:` picks + review Submit/Cancel; the multi-select `aqt:` toggle still dispatches a bare digit (a filed fast-follow — AUQ is NOT globally fixed).** Validated against Claude Code v2.1.168 terminal behavior.
- **AUQ restart-recovery (D2)** — D3-β keeps a live card's *in-memory* pick tokens un-killable while the poller observes it, but a bot **restart** wipes them; the published card keeps its old keyboard with dead token strings, so the first tap hits `peek_none` and (pre-D2) degraded to the honest "tap again" modal for the card's whole life. D2 persists the per-token mint intent to a new leaf store (`pick_intent.py` → `pick_intent.jsonl`, written at the fresh `aqp:` single-select/Submit render; `aqt:` toggles excluded) so the `peek_none` / `expired` branches RECOVER and re-dispatch via `pick_token.recover_and_consume`. The store is keyed by the **token string** (a stale tap for form A can't read a newer same-key row B) and is kept **separate** from `auq_action_ledger.jsonl` — that ledger stays the 24h durable single-use authority; writing recovery state into its latest-wins `(route_hash, fp8, opt)` key would clobber a `dispatched` row and re-open double-dispatch. Recovery is **row-scoped**: a `_recovery_row_reservations[cache_key]` serialises concurrent sibling taps, a per-sibling action-ledger guard makes single-select single-use across siblings even across a crash, and a `consume_row` tomb is hygiene. It reproduces the live path's full **owner + `reject_stale_window_callback`** auth pair (the historic `peek_none` branch had neither) plus a callback-payload parity check against the stored intent, and **read-TTL-free** source parity (`auq_source.read_side_file_for_recovery`, comparing `_canonical_dict_fingerprint` — never the 12-hex `input_fingerprint`; pane fallback only when the side file is genuinely gone). The decisive invariant: recovery fires only on **positive proof of in-memory loss** (no `_pick_token_cache` row at the reconstructed `cache_key`) — a live row means the normal path owns it, a tombstoned row means this process just consumed it — so D2 is strictly the restart net and never double-handles the live path. The `accepted` claim is written INSIDE the row reservation (no release-then-claim gap), with a re-check of the cache-row + sibling proofs before it. Render/callback-path state only — NOT a `route_runtime` field; pull-only, no observer (c313657 stays forbidden). Tombed at `forget_ask_tool_input` (AUQ/EPM resolution + the `/clear` race via the OLD-window `forget_ask_tool_input(wid)` call) and `clear_topic_state`; orphan-safety is the recovery-time form/source re-validation + the 24h GC. Off-contract residual: a `jsonl_cache`-minted card DECLINES (its in-process getter is wiped on restart). The form fingerprint is now **cursor-blind on EVERY screen** — `AskUserQuestionForm._canonical_repr` omits the per-option cursor bit UNCONDITIONALLY (not just when `is_review_screen`); `auq_source._pane_fingerprint` shares that canonical so the pane source fingerprint collapses in lockstep. The cursor-blind fingerprint stays load-bearing under the v2.1.168 navigate-to-target dispatch: the bot MOVES the cursor to the target before committing, so the form identity must NOT change as the cursor moves (else the nav-verify re-parse would no longer match the minted fingerprint and every pick would bail `not_advanced`). A moved cursor — Submit↔Cancel on the review screen OR any option on a non-review picker — no longer rotates the pick token, so D2 recovery survives a cursor move on **every** screen; **the former D3-γ non-review DECLINE is RETIRED** (the non-review twin of the PR #28 review-screen fix). The review-Submit live + recovery guards share the cursor-blind `AskUserQuestionForm.review_submit_dispatchable` predicate (anchored on `is_review_screen` + option #1 + the literal `REVIEW_SUBMIT_LABEL` + the minted label; verified on Claude Code v2.1.161/.167/.168). The `_pane_fingerprint` ⇄ `_canonical_repr` shared-canonical coupling is load-bearing — guarded by the fingerprint-EQUALITY-across-cursor-move tests for BOTH the review screen and non-review pickers.
- **AUQ multi-select toggles** — multi-select option buttons use `aqt:<route_hash>:<fp8>:<opt>:<token>` and route to the interactive executor. `aqt:` validates the live token/window/form, dispatches a bare digit to tmux with no Enter, then re-renders from the pane. Toggles are not ledgered and do not consume sibling tokens; final Submit/Cancel is reached by Tab on the Claude Code review screen and reuses the existing `aqp:` pick/ledger flow. **The `aqt:` toggle still dispatches a bare digit** — under v2.1.168 the single-select `aqp:` pick/Submit moved to the navigate-to-target + Enter model (see the dispatch bullet above), but the multi-select toggle was left on the bare digit as a documented **fast-follow** (so AUQ is fixed for single-select picks + review Submit/Cancel, NOT globally). Converting `aqt:` to the navigate-to-target model is filed.
