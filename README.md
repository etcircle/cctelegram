# cc-telegram

A Telegram ↔ Claude Code bridge for running Claude sessions from Telegram forum topics.

Each Telegram topic maps to one tmux window running one Claude Code process. The terminal remains the source of truth, and Telegram becomes the remote control / notification layer.

## What it does

- **Topic-based sessions** — one Telegram topic = one tmux window = one Claude session.
- **Hook-based session tracking** — Claude Code `SessionStart` writes `session_map.json`, so `/clear` and resumed sessions stay attached to the right topic.
- **AskUserQuestion descriptions and multi-select toggles** — a `PreToolUse` hook captures the structured `AskUserQuestion` payload before Claude renders the picker, so each option's full description shows in Telegram right away. Single-select options submit through the restart-safe `aqp:` pick flow; multi-select options toggle with non-ledgered `aqt:` bare-digit callbacks, then final Submit/Cancel reuses the review-screen `aqp:` flow. A single-select pick / review Submit **navigates the live cursor to the tapped option with arrow keys, then presses Enter**, and only records the dispatch once the pane confirms the expected advance — version-stable on Claude Code v2.1.168, where a bare digit no longer reliably selects. (The `aqt:` multi-select toggle still uses a bare digit.)
- **Live prose before interactive prompts** — when Claude writes explanatory prose in the same turn as an `AskUserQuestion` / `ExitPlanMode`, Claude Code buffers the whole turn in the session JSONL until the prompt resolves, so without help the Telegram user would see only the picker and choose blind. A lightweight `MessageDisplay` hook captures that prose live (before the picker blocks) so the bot can deliver it ahead of the picker card.
- **Streaming output** — assistant text, thinking, tool use/result summaries, interactive prompts, and local command output flow into Telegram.
- **Per-route queues** — each `(user_id, thread_id, window_id)` has its own worker, so one noisy topic does not stall another.
- **Run-state digest** — compact activity digests show tool activity, context-window percentage, and busy/waiting state.
- **Reply context** — Telegram replies/quotes are injected into Claude with fenced, role-aware context for text, voice, photo, and document messages.
- **Photos and voice** — photos are forwarded as base64 image blocks; voice notes are transcribed through OpenAI-compatible transcription.
- **Attention cards** — when Claude is waiting on you and the structured picker can't be delivered to the topic, a single bold "Claude is waiting for you" card is pushed (notified once per episode, then silently kept current).
- **SQLite provenance** — outgoing Telegram messages are indexed for safer reply-context resolution.
- **Reactive broken-topic fallback** — if Telegram says a topic is gone/closed/forbidden, the bot falls back to DM rather than silently dropping Claude output.

## Quick start

Zero to working bot in a handful of commands:

```bash
git clone https://github.com/etcircle/cc-telegram.git && cd cc-telegram
uv tool install --force .
mkdir -p ~/.cc-telegram && $EDITOR ~/.cc-telegram/.env  # TELEGRAM_BOT_TOKEN, ALLOWED_USERS, TMUX_SESSION_NAME, CLAUDE_COMMAND
cc-telegram hook --install
cc-telegram doctor       # verify all green
# Then either: cc-telegram (foreground) or install the launchd plist
```

## Requirements

- Python 3.12+
- `uv`
- `tmux`
- Claude Code CLI (`claude`) in `PATH`
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Telegram supergroup with forum topics enabled

## Install

```bash
git clone https://github.com/etcircle/cc-telegram.git
cd cc-telegram
uv sync --all-extras
```

## Configure

Create `~/.cc-telegram/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

Core variables:

- `TELEGRAM_BOT_TOKEN` — required; from BotFather.
- `ALLOWED_USERS` — required; comma-separated Telegram user IDs.
- `CC_TELEGRAM_DIR` — config/state directory; default `~/.cc-telegram`.
- `TMUX_SESSION_NAME` — tmux session driven by the bot; default `cc-telegram`.
- `CLAUDE_COMMAND` — command used for new windows; default `claude`.
- `CLAUDE_CONFIG_DIR` — Claude config root; projects default to `$CLAUDE_CONFIG_DIR/projects`.
- `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` — explicit Claude projects directory override.
- `MONITOR_POLL_INTERVAL` — JSONL poll interval; default `2.0`.
- `CC_TELEGRAM_BROWSE_ROOT` — directory picker root; default `~`.
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` — optional voice transcription provider.

Useful behavior knobs:

- `CC_TELEGRAM_SHOW_USER_MESSAGES` — echo user messages from tmux; default `true`.
- `CC_TELEGRAM_SHOW_TOOL_CALLS` — show tool use/result stream; default `true`.
- `CC_TELEGRAM_SHOW_HIDDEN_DIRS` — show dot-directories in picker; default `false`.
- `CC_TELEGRAM_TOOL_SUMMARY_MAX_CHARS` — max input shown in `**Tool**(...)`; default `40`.
- `CC_TELEGRAM_AGENT_PROMPT_PREVIEW_CHARS` — subagent dispatch excerpt; default `400`.
- `CC_TELEGRAM_REPLY_CONTEXT` — inject reply/quote context; default `true`.
- `CC_TELEGRAM_QUOTE_INJECTION_MAX_CHARS` — max quoted text injected into Claude; default `1600`.
- `CC_TELEGRAM_AGGREGATOR_DEBOUNCE_SECONDS` — media/caption coalescing window; default `1.5`.
- `CC_TELEGRAM_AGGREGATOR_MAX_ATTACHMENTS` — per-bundle attachment cap; default `10`.
- `CC_TELEGRAM_MAX_ATTACHMENT_SIZE_BYTES` — document download cap; default `20971520`.
- `CC_TELEGRAM_CONTEXT_PCT_THRESHOLD` — context-% digest threshold; default `80`.
- `CC_TELEGRAM_CONTEXT_IN_MESSAGE_FOOTER` — per-turn token footer; default `true`.
- `CC_TELEGRAM_MESSAGE_REFS_RETENTION_DAYS` — provenance retention; default `30`.
- `CC_TELEGRAM_MESSAGE_REFS_DB_PATH` — SQLite path; default `$CC_TELEGRAM_DIR/message_refs.db`.
- `CC_TELEGRAM_MESSAGE_REF_TEXT_MAX_CHARS` — stored body cap; default `4000`.

### State files

Under `$CC_TELEGRAM_DIR` (default `~/.cc-telegram/`):

- `state.json` — thread bindings, window states, display names, read offsets.
- `session_map.json` — hook-generated `window_id → session` mapping (written by the `SessionStart` hook).
- `monitor_state.json` — JSONL byte offsets per tracked session (incremental-read progress).
- `interactive_state.json` — persisted picker message ids + AUQ context markers (survives bot restart so a `launchctl kickstart` doesn't lose interactive state).
- `auq_pending/<session_id>.json` — `PreToolUse` side files (one per active AUQ; mode `0600` under directory mode `0700`). Multi-select `aqt:` toggles keep the side file alive; it is cleaned when the AUQ `tool_result` runs `forget_ask_tool_input`, on session replacement, or by startup GC.
- `auq_action_ledger.jsonl` — restart-safe write-ahead ledger for AUQ option-pick dispatches (mode `0600`; append-only JSONL; latest line per `(route_hash, fp8, opt)` key wins; the callback handler consults this to detect duplicate taps after a process restart so the same pick is never committed twice). States: `accepted → dispatched` (confirmed advance), `not_advanced` (pre-commit bail — a re-tap falls through), `commit_unconfirmed` (Enter sent, advance unconfirmed — refresh-only), and `released` (the AUQ resolved — appended for the window's rows only on a tool_result-confirmed resolution, at the AUQ tool_result branch in the bot's message handler and by the startup reconciler's positive-proof block, so a re-asked identical question is dispatchable again; generic teardown such as `/clear` or session replacement never releases). 24h retention is enforced on read (load + lookup); the file is rewritten only by over-cap compaction.
- `pick_intent.jsonl` — D2 restart-recovery: durable per-callback-**token** AUQ pick mint-intent store (mode `0600`; append-only JSONL row + tombstone lines; 24h retention + compaction). Written at the fresh single-select / review-Submit (`aqp:`) render; after a bot restart wipes the in-memory pick tokens, the callback handler reads it to RECOVER and re-dispatch the first token-less tap on a still-open card (row-scoped single-use; owner + stale-window auth; read-TTL-free source parity). Deliberately **not** the `(route_hash, fp8, opt)`-keyed action ledger above — writing recovery state there would clobber a `dispatched` row and re-open double-dispatch. Tombed on AUQ/EPM resolution, `/clear`, and topic close.
- `md_hook_settings.json` — bot-managed Claude Code settings file registering the `MessageDisplay` hook. Passed to bot-launched sessions via `claude --settings`, so the live-prose hook is scoped to the bot's own windows (it is never written into the global `~/.claude/settings.json`). Re-written on startup and on each window launch if its content drifts.
- `msg_display/<session_id>.ndjson` — `MessageDisplay` live-prose capture (one file per session keyed by the transcript filename, so it is resume-safe; mode `0600` under directory mode `0700`). The hook appends each streaming `delta`; the bot accumulates them into completed prose, posts it before the picker card, and (in the same file) records shown-live markers used to dedup the post-resolution copy. Removed on prompt resolution / session replacement / `/clear` / topic close, with a 1h startup GC backstop.
- `message_refs.db` — SQLite provenance index for safer reply-context resolution (path overridable via `CC_TELEGRAM_MESSAGE_REFS_DB_PATH`).
- `log-archive/` — gzipped log rotations (only present if the rotation LaunchAgent is installed; see "Log rotation").

All state files are safe to delete — the bot re-creates what it needs on next start (you will lose interactive picker continuity and bound topic mappings).

## Voice transcription

Voice notes are transcribed via a standard OpenAI `POST $OPENAI_BASE_URL/audio/transcriptions` call with `Authorization: Bearer $OPENAI_API_KEY`. Point `OPENAI_BASE_URL` at anything that speaks that shape:

- `https://api.openai.com/v1` — the default.
- `https://openrouter.ai/api/v1` — OpenRouter exposes whisper-1 over the same API.
- A local LiteLLM, vLLM, or other OpenAI-compatible gateway.

If your backend doesn't natively speak OpenAI's STT shape (e.g., a local `whisper.cpp` server with its `/inference` endpoint), front it with a small shape-translating proxy. A 130-line stdlib-only example lives next to this repo as [`whisper-openai-proxy/`](../whisper-openai-proxy) — clone or copy, point `OPENAI_BASE_URL` at it.

## Install the Claude Code hook

```bash
uv run cc-telegram hook --install
```

This writes/updates `~/.claude/settings.json` with two managed hook entries:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "cc-telegram hook", "timeout": 5 }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion",
        "hooks": [
          { "type": "command", "command": "cc-telegram hook", "timeout": 2 }
        ]
      }
    ]
  }
}
```

The `SessionStart` hook writes `session_map.json` so the bot can route messages back to the right tmux window. The `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured question payload before Claude renders the picker — see the next section.

### AskUserQuestion (AUQ) descriptions

When Claude Code calls `AskUserQuestion`, the option descriptions are not visible in the terminal pane until the user picks an option (Claude Code buffers `tool_use` until `tool_result`). The PreToolUse hook captures the structured `tool_input` and writes it to:

```
<CC_TELEGRAM_DIR>/auq_pending/<session_id>.json   (mode 0600; directory mode 0700)
```

The bot reads the side file at picker render time so the Telegram context message shows each option's full description right away, not after-the-fact. Multi-select AUQs render selected/unchecked/off-screen state and use `aqt:` callbacks to send a bare digit to tmux for each toggle; those toggles are reversible and not written to the AUQ ledger. The user then presses Tab to Claude Code's review screen, where Submit/Cancel uses the existing `aqp:` pick path and restart-safe ledger.

The single-select `aqp:` pick and the review-screen Submit/Cancel **navigate the live cursor to the tapped option with arrow keys and then press Enter** — the version-stable commit — and record the ledger `dispatched` lock only after re-parsing the pane confirms the form made the exact expected advance. On Claude Code v2.1.168 a bare digit no longer reliably selects (in the notes side-panel picker variant it only moves the cursor), so dispatch decouples from the digit entirely; arrows are pure navigation in every variant and `Enter to select` is in every picker's footer. A keystroke that is sent but whose advance can't be confirmed is recorded `commit_unconfirmed` (refresh-only, never auto-re-sent), and a pre-commit bail (cursor not found / send failed / cursor didn't land on the target) is `not_advanced` (retryable) — so a tap never over-advances and never falsely locks with "Action already received". The multi-select `aqt:` toggle still sends a bare digit (that path is unchanged for now). (Validated against Claude Code v2.1.168 terminal behavior.)

Side files are:

- Auto-created on each AUQ; the directory and files are mode `0700`/`0600`.
- Preserved across multi-select `aqt:` toggles and final Submit keypresses.
- Cleaned up when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when a session is replaced, or by startup GC.
- Garbage-collected on bot startup (any stale entries older than the TTL).
- Safe to delete the directory at any time; it is re-created on the next AUQ.

If the PreToolUse hook entry is missing from `~/.claude/settings.json`, the bot logs a one-time startup warning and falls back to pane-only descriptions. Re-run `cc-telegram hook --install` to repair.

### Live prose before AskUserQuestion / ExitPlanMode (MessageDisplay hook)

`cc-telegram hook --install` manages only the `SessionStart` and `PreToolUse` entries above. A third hook — Claude Code's `MessageDisplay` event — is managed **automatically by the bot** and needs no manual install. It is **not** written into the global `~/.claude/settings.json`; instead the bot writes a small settings file and passes it only to the sessions it launches:

```
<CC_TELEGRAM_DIR>/md_hook_settings.json    → claude --settings <that file>
```

So the hook fires only for the bot's own windows (it merges with the global `SessionStart` / `PreToolUse` hooks). The hook itself is a tiny stdlib-only appender (run directly by the Python interpreter, never importing the package) so it stays well under the streaming-display latency budget. It appends each streaming `delta` of an assistant message to:

```
<CC_TELEGRAM_DIR>/msg_display/<session_id>.ndjson   (mode 0600; directory mode 0700)
```

When Claude writes prose in the same turn as an `AskUserQuestion` / `ExitPlanMode`, Claude Code buffers the whole turn in the session JSONL until the prompt resolves — so the explanatory prose would otherwise reach Telegram only after the user already chose. The bot accumulates the captured `delta`s into the completed prose and posts it before the picker card, then dedups the post-resolution JSONL copy so the prose appears exactly once. Capture files are removed on prompt resolution / session replacement / `/clear` / topic close, with a 1h startup GC backstop; the directory is safe to delete at any time.

If the bot cannot write the settings file (e.g. an unwritable config dir), it logs a one-time startup warning and live prose silently falls back to post-resolution delivery — no crash, the picker still works.

## Run

```bash
uv run cc-telegram
```

If installed as a tool:

```bash
cc-telegram
```

For day-to-day use, run it inside tmux or a process supervisor.

## Restart the service

If the bot runs under launchd (the recommended setup on macOS), restart it with:

```bash
launchctl kickstart -k gui/$(id -u)/com.cc-telegram
```

## Log rotation

`launchd.err.log` and `launchd.out.log` are written by launchd's
stderr/stdout redirect, not by Python's logging — so the bot can't
rotate them itself. A small LaunchAgent handles rotation: every 30
minutes it checks both files, gzips a dated copy into
`~/.cc-telegram/log-archive/` if either exceeds 50MB, and truncates
the original in place (safe under the bot's `O_APPEND` write).
Archives older than 14 days are deleted automatically. Install with:

```bash
bash bin/install-log-rotate.sh
```

The script is idempotent — re-running replaces the existing agent.
Override thresholds via env in the plist `EnvironmentVariables` block
(`CC_TELEGRAM_LOG_ROTATE_THRESHOLD_MB`,
`CC_TELEGRAM_LOG_ROTATE_MAX_AGE_DAYS`).

Force a rotation pass now:

```bash
launchctl kickstart gui/$(id -u)/com.cc-telegram.log-rotate
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.cc-telegram.log-rotate
rm ~/Library/LaunchAgents/com.cc-telegram.log-rotate.plist
```

Without this, a crash-loop (e.g. a startup AttributeError under
`KeepAlive=true`) can balloon `launchd.err.log` to hundreds of
megabytes and trigger Telegram `getUpdates` rate-limiting via the
restart spam. The rotation cap also caps the blast radius.

## Config directory override

Default config dir: `~/.cc-telegram`.

Override with the `CC_TELEGRAM_DIR` env var:

```bash
CC_TELEGRAM_DIR=/path/to/state cc-telegram
```

Useful for testing or running multiple profiles against the same install.

## Recommended daily-driver `.env`

Only use this if the bot runs on a machine you trust and `ALLOWED_USERS` is locked to you. `--dangerously-skip-permissions` means Claude can act without local confirmation.

```ini
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=<your_id>
CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
MONITOR_POLL_INTERVAL=1.0
OPENAI_API_KEY=sk-...
CC_TELEGRAM_BROWSE_ROOT=~/dev
# CC_TELEGRAM_SHOW_TOOL_CALLS=false
# CC_TELEGRAM_SHOW_USER_MESSAGES=false
```

## Test

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
uv run pytest -m scenario -q          # behavior floor (tests/scenarios/)
bin/post-wave-check.sh                # repo health diff (LoC + brittleness signals)
```

`tests/scenarios/` holds the black-box behavior floor: each file drives a
single user-visible scenario through the real handler stack (no
monkeypatch of handler internals in test bodies). See
`tests/scenarios/README.md` for the scenario → behavior map.

## Repository layout

```text
src/cctelegram/                     core package
src/cctelegram/handlers/            Telegram interaction layer
  attention.py                      end-of-turn attention cards
  inbound_aggregator.py             caption/media/photo+text bundler
  reply_context.py                  Telegram reply/quote → Claude context
  message_queue.py                  per-route FIFO worker
  message_sender.py                 safe send/edit/delete with MarkdownV2 fallback
  status_polling.py                 poll loop + typing-action loop
  interactive_ui.py                 AskUserQuestion / ExitPlanMode / permission UI
  directory_browser.py              directory + session picker
  history.py                        /history paginator
  cleanup.py                        centralized topic teardown
src/cctelegram/message_refs.py            SQLite provenance table
src/cctelegram/session_monitor.py         JSONL tail + TranscriptEvent dispatch
src/cctelegram/transcript_parser.py       JSONL → ParsedEntry / TranscriptEvent
src/cctelegram/route_runtime.py           per-route run-state / context-usage / idle-clear authority
src/cctelegram/transcript_event_adapter.py  TranscriptEvent → route_runtime adapter
src/cctelegram/md_capture.py              MessageDisplay live-prose reader/accumulator + capture-settings/teardown
src/cctelegram/_md_display_appender.py    tiny stdlib MessageDisplay hook (appends deltas; never imports the package)
tests/                              pytest suite
tests/scenarios/                    black-box behavior floor (@pytest.mark.scenario)
bin/post-wave-check.sh              repo-health diff for the architecture campaign
.claude/rules/                      architecture notes loaded by Claude Code
```

## License

MIT — see [LICENSE](LICENSE).
