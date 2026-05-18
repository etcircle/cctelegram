# Scenario test floor (Wave A)

Black-box behavior tests at the public Telegram seam. Each file maps to
one user-visible scenario; assertions reach only at the seams:

  - inbound `Update` → real handler (`bot.text_handler`, `callback_handler`,
    …) → fake tmux + fake bot,
  - outbound `NewMessage` → `handle_new_message(msg, fake_bot)` → through
    `message_queue` worker → fake bot.

No test body monkeypatches handler-module internals. Fixture-side state
reset lives in `tests/conftest.py` (a known Wave B target — `message_queue`
et al. lack `reset_for_tests()` seams today).

Run: `uv run pytest -m scenario -q`.

## Scenario → behavior map

| Test file | Behavior it asserts |
| --- | --- |
| `test_unbound_topic_first_message.py` | First text in an unbound topic opens the directory browser and stashes the text in `_pending_thread_*`. Named topic with dead binding unbinds and warns. |
| `test_tool_lifecycle.py` | One tool turn renders as one digest send (after `_finalize_activity_digest`) + one assistant-text send. `tool_use` / `tool_result` never produce direct messages under V2. |
| `test_interactive_prompt_safety.py` | Wrong-user click on an `aqp:` token is rejected with "Not your card." and does NOT consume the token. Expired / stale-fingerprint clicks refresh the card without sending a digit to tmux. |
| `test_media_group.py` | Telegram media-group photos coalesce into one bundle; caption rides item 1 only; subsequent items skip the caption to avoid duplication. |
| `test_tmux_restart.py` | Stale window IDs re-resolve via display names after tmux restart. **Includes one `xfail`** that surfaces a real ordering bug in `resolve_stale_ids()` (see "Wave A findings" below). |
| `test_route_busy_lifecycle.py` | c313657 regression: a transcript event after `IDLE_CLEARED` drops `status_polling._idle_state[key]` via the `register_activity_callback` channel. Inbound-sent and full-turn state walks too. |
| `test_topic_close_cleanup.py` | Closing a topic kills the bound tmux window and unbinds. Idempotent against missing bindings / already-killed windows. |
| `test_voice_upload_transcribe.py` | Voice → `transcribe_voice` substrate → `aggregator_offer_voice` with the transcription. Echo bubble sent. Missing API key surfaces a warning. |
| `test_document_upload.py` | Document → download → `aggregator_offer_document` (bound) or pending-attachment stash (unbound). Oversized files are rejected. |
| `test_slash_command_flush.py` | `forward_command_handler` flushes the per-route aggregator bundle BEFORE forwarding the slash command, preserving arrival order at the pane. |
| `test_kill_mid_tool_use.py` | `/kill` kills the window, unbinds, runs `clear_topic_state` (no leftover entries in `message_queue` topic-keyed maps), confirms with display name. |
| `test_clear_mid_stream.py` | `/clear` rotates `session_id` to empty; subsequent `NewMessage` carrying the old session_id no longer routes to this topic. |
| `test_stale_pending_replacement.py` | A new unbound-topic message takes ownership of `_pending_thread_id`, marks the previous thread as ignored-stale, and a late cancel from the old thread no longer clobbers the new pending payload (`bot.py:273-303`). |
| `test_screenshot_stale_window.py` | Screenshot keyboard taps against killed / rebound windows are rejected ("Window not found" / "Stale controls") before any tmux keystroke. |
| `test_topic_rename.py` | Topic rename propagates to `tmux_manager.rename_window` + `session_manager.window_display_names`. Idempotent against same-name renames. |
| `test_topic_broken_recovery.py` | `probe_topic_liveness` cleans the orphan window when Telegram returns `TOPIC_NOT_FOUND` on the heartbeat. Healthy topics are left alone. |

## Existing-test triage (Wave A classification)

Wave A does NOT delete or rewrite any of the 775 pre-existing tests; it
classifies them so the campaign knows which ones are load-bearing and
which are scenario-overlap candidates.

| Bucket | Files (and why) |
| --- | --- |
| **(1) Keep — protected invariants** | `tests/cctelegram/handlers/test_busy_indicator.py` (parallel-tools / 1M context latch / sidechain replay / WAITING_ON_USER restoration), `test_status_polling.py` + `test_status_polling_wave2.py` (V2 typing separation), `test_pending_route_payload.py` (owner replacement + ignored-stale-thread machinery), `test_stale_window_callbacks.py` (ordering: stale rejection before tmux lookup), `test_terminal_parser.py` / `test_transcript_parser.py` (pure parsers — no scenario overlap), `test_message_queue.py` (digest invariants Wave A doesn't yet cover end-to-end), `test_interactive_ui.py` (mint/peek/consume mechanics under sidechain), `test_session_monitor.py` (poll cycle invariants). |
| **(2) Replace — scenario overlap; keep for now** | `test_forward_command.py` overlaps with `test_slash_command_flush`. `test_kill_command.py` overlaps with `test_kill_mid_tool_use`. Parts of `test_pending_route_payload.py` overlap with `test_stale_pending_replacement` (but keep for the file-deletion invariants the scenario doesn't cover). |
| **(3) Delete — incidental coupling** | **None in Wave A.** Wave B/C may revisit after deepening lands. |

Triage rule of thumb: a test that monkeypatches `handlers.*._<private>`
state and has a scenario covering the same user-visible behavior is a
*candidate* for Wave B/C deletion. The named "(1) Keep" tests are
explicit no-touch per the kickoff handoff.

## Wave A findings (surfaced by the scenario floor)

These are real architectural smells the scenario tests surfaced. They
do NOT block Wave A merge — Wave B/C should address them.

  1. **`SessionManager.resolve_stale_ids` ordering bug.**
     `test_tmux_restart::test_stale_window_id_remapped_by_display_name`
     `xfail`s: the function pops `window_display_names[old_id]` during
     window_states migration *before* thread_bindings migration runs,
     so the bindings loop's display-name lookup misses and the binding
     is silently dropped. Fix: hold the display-name snapshot for the
     whole function, or migrate bindings before popping.
  2. **`busy_indicator._open_tools` only cleared via `teardown_route`.**
     If `_open_tools[route]` got seeded (e.g. via startup replay) but
     no message_queue queue ever opened for the route, `/kill` /
     topic-close don't reach `busy_indicator.clear_route`. The
     route's open_tools survives. (Touched in Wave A by relaxing the
     direct assertion in `test_kill_mid_tool_use`; full coverage will
     come via the Wave B `RouteRuntime` snapshot interface.)
  3. **`message_queue` lacks a `reset_for_tests()` seam.** The fixture
     in `tests/conftest.py` clears 20+ module-level dicts to give each
     scenario a clean slate. Wave B's `RouteRuntime` should consolidate
     this into a single reset call.
  4. **`inbound_aggregator`, `status_polling`, `interactive_ui` same as
     above** — no `reset_for_tests()` seam; the fixture pokes module
     state directly. Each is a Wave B/C candidate for a real seam.

The kill criterion ("scenarios can't be written without monkeypatching
internals") was **not** triggered: every scenario test body assertion
operates on public surfaces. The fixture-side state reset is
test-infrastructure scaffolding, documented as such.
