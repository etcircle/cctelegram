# Changelog

All notable changes to cc-telegram. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project's package version is bumped per release, not per deploy (see the `--no-cache` note in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

## [0.2.0] — 2026-06-24

The "busy-signal + AskUserQuestion bridge" release: ~190 commits since v0.1.0 making Telegram a
faithful mirror of what Claude Code is actually doing — interactive prompts, background work, and
run-state — plus a deployment-docs pass so another operator (or code agent) can stand the bot up
from scratch.

### Added
- **Cross-topic dashboard** (`/dashboard`) — one owner+chat-scoped overview message listing every
  bound topic grouped needs-attention-first (🔔 / 🟡 / ⚪), repainted by the status poller; `/dashboard pin` opt-in.
- **Per-user output verbosity** (`/settings`) — `verbose` / `standard` / `compact` / `quiet` presets
  plus per-knob overrides (tool-line length, done-card policy, sub-agent cards, 👤 echo, 📊 footer),
  persisted per user in `state.json`. Production default is `standard`.
- **"🔔 Waiting on you" detection** via a new matcher-less `Notification` hook + `notify_pending/`
  side files — covers permission/approval gates (including the Workflow tool's Bash-approval gate)
  that leave no JSONL trace, with a persistent, audible decision card.
- **Live prose before interactive prompts** via a bot-managed `MessageDisplay` hook
  (`md_hook_settings.json` + `msg_display/` capture) — explanatory prose written in the same turn as
  an `AskUserQuestion` / `ExitPlanMode` is delivered *before* the picker, not after resolution.
- **ExitPlanMode plan body before the picker card** (findings → 📋 Plan → card ordering).
- **Background-agent + Workflow run-state** — `run_in_background` Agents and the `Workflow` tool now
  light typing + 🟡 Busy while they work (GH #44 snapshot projection + the ISSUE-6 Workflow bracket),
  with `↳` sub-agent display cards that collapse on completion, and a startup reconciler that
  re-lights still-running background work across a restart.
- **Background-jobs decoration** (GH #43) — `⏳ N background job(s)` on collapsed done-cards + the
  dashboard glyph, parsed from the pane.
- **Docs / deploy ergonomics** — `docs/DEPLOYMENT.md` (end-to-end setup + the `--no-cache` upgrade
  recipe + troubleshooting), top-level `AGENTS.md`, and `bin/install-service.sh` to generate + load
  the `com.cc-telegram` LaunchAgent. Log-rotation LaunchAgent (`bin/install-log-rotate.sh`).
- **Post-turn digest collapse** — the activity card collapses to a one-line summary when the turn
  ends; per-sub-agent cards collapse the same way.

### Changed
- **`route_runtime` is now the sole run-state / context-usage / idle-clear authority** — the old
  `busy_indicator` and observer/callback fan-out (root cause of bug c313657) were removed in favor of
  a pull-only per-route state machine with immutable snapshots.
- **AskUserQuestion pick dispatch navigates the cursor to the target and presses Enter** (validated
  against Claude Code v2.1.168, where a bare digit no longer reliably selects), recording the ledger
  `dispatched` lock only after the pane confirms the expected advance. Restart-safe via an
  append-only action ledger + a durable mint-intent store.
- Interactive-surface teardown is now **parent-only (sidechain-gated)** — a background agent
  narrating no longer tears down the parent's live AUQ/EPM/Permission card.

### Fixed
- **Typing indicator stayed dark for the full 30-min TTL** while a background agent worked
  (parent idle) — `BG_RUNNING` now clears the projected-busy 🔔 on the agent's next heartbeat
  (scoped to the sole-live-plain-Agent shape for safety).
- **AUQ "📋 full details" ctx-card ~28× duplication** in a busy topic while a background Workflow ran.
- **AUQ picker-card churn / duplicate cards** on long-open cards in busy topics (pane↔pane drift
  no-op + transient-edit-keep).
- **Claude Code v2.1.170 interactive-UI detection drift** (EPM footer `ctrl-g`→`ctrl+g` + a new
  "Settings Warning" marker) that hid both the picker and the findings prose.
- Out-of-order JSONL tool pairing / stuck-route eligibility (GH #42).
- Numerous AUQ card-liveness, source-parity, and restart-recovery correctness fixes.

### Notes
- The package version is bumped per release, not per deploy. Always deploy with
  `uv tool install --force --no-cache .` (the wheel cache is version-keyed; without `--no-cache`,
  same-version redeploys reinstall a stale wheel). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## [0.1.0] — 2026-05-17

Initial tagged release: Telegram ↔ Claude Code bridge, topic-only architecture
(1 Topic = 1 tmux window = 1 Claude session), `SessionStart` hook session tracking,
per-route message queues, MarkdownV2 output, streaming tool/thinking/status, photos + voice,
reply context, and SQLite provenance.

[0.2.0]: https://github.com/etcircle/cc-telegram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/etcircle/cc-telegram/releases/tag/v0.1.0
