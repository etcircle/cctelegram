# Deploying cc-telegram

cc-telegram is a Telegram bot that bridges Telegram Forum topics to Claude Code sessions running in tmux windows. The model is strictly **1 Topic = 1 tmux window = 1 Claude Code session**, keyed internally by tmux window id (`@0`, `@12`). The terminal stays the source of truth; Telegram is the remote control + notification layer.

This guide takes you from a clean machine to a running, redeployable bot with zero prior context. The launchd parts are macOS-specific; the manual run works on any Unix.

---

## 1. Prerequisites

Install and verify each of these first.

| Requirement | Why | Check |
|---|---|---|
| **Python 3.12+** | `pyproject.toml` sets `requires-python = ">=3.12"` | `python3 --version` |
| **`uv`** (Astral) | Builds the wheel and installs the `cc-telegram` console script; also the dev runner | `uv --version` |
| **`tmux`** | The bot drives one tmux session (default name `cc-telegram`); one window per topic | `tmux -V` |
| **Claude Code CLI (`claude`)** | The bot shells out to `claude` per window. **It must be independently authenticated** — cc-telegram manages NO Anthropic credentials (zero `ANTHROPIC_*` handling in the code) | `claude --version`, then run `claude` once interactively to log in |
| **Telegram bot token** | From @BotFather | — |
| **Your numeric Telegram user id** | For `ALLOWED_USERS`; get it from @userinfobot | — |
| **A Telegram supergroup with Forum (Topics) mode ON** | The bot is **topic-only**: no DM/General routing, no `/list` | add the bot as a member/admin |
| **(Optional) OpenAI-compatible STT key** | Only for voice-note transcription | — |

> **Anthropic auth is out of cc-telegram's scope.** The bot launches `claude` in each tmux window. If Claude Code is not logged in (or `ANTHROPIC_API_KEY` is not exported into the bot's launch environment), the window's `claude` errors on auth and the topic shows opaque/empty output. `ANTHROPIC_API_KEY` is NOT scrubbed by the bot (only `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, `OPENAI_API_KEY` are in `SENSITIVE_ENV_VARS`), so if you set it in the bot's environment it passes through to launched windows.

---

## 2. Clone

The deploy recipe installs **from a local checkout** (the `.` in `uv tool install ... .`), so you need the working tree on disk.

```bash
git clone https://github.com/etcircle/cc-telegram.git
cd cc-telegram
```

---

## 3. Install the `cc-telegram` console script

There are two modes. Pick one.

### Mode A — Install as a tool (production / the deploy path)

```bash
uv tool install --force --no-cache .
command -v cc-telegram        # expect ~/.local/bin/cc-telegram
# If not on PATH:  export PATH="$HOME/.local/bin:$PATH"   (add to your shell profile)
```

`uv tool install` builds the wheel (hatchling, `packages=["src/cctelegram"]`) and drops the `cc-telegram` entry point (defined in `pyproject.toml` `[project.scripts]` as `cctelegram.main:main`) into `~/.local/bin`. After this, **bare `cc-telegram ...` works on PATH.**

> **Why `--no-cache` is mandatory.** uv's build/wheel cache is keyed on the package *version*, and the version is **not bumped on every code change**. So when you redeploy the same version, `--force` alone happily reinstalls the *cached old wheel* — the command exits 0 but your new code does NOT ship. `--no-cache` forces a fresh build. This is the single highest-cost footgun in this repo; see section 8.

### Mode B — Run from source (development)

```bash
uv sync --all-extras          # creates the dev .venv with dev extras (pyright/pytest/ruff)
uv run cc-telegram ...        # ALWAYS prefix with `uv run`
```

> `uv sync` does **NOT** put `cc-telegram` on PATH. In dev mode every command is `uv run cc-telegram ...` (`uv run cc-telegram doctor`, `uv run cc-telegram hook --install`, etc.).

---

## 4. Config directory and `.env`

State + config live under **`$CC_TELEGRAM_DIR`** (default `~/.cc-telegram`, resolved by `utils.app_dir()` and the `Config` constructor in `config.py`).

`.env` loading priority for the **running bot** (`config.py`): a local `./.env` in the cwd is loaded first (wins), then `$CC_TELEGRAM_DIR/.env`.

> **Caveat — `cc-telegram doctor` is narrower:** doctor reads only the already-exported environment plus the config-dir `$CC_TELEGRAM_DIR/.env` — it does **not** read a cwd `./.env`. Keep your `.env` in `$CC_TELEGRAM_DIR` (as the steps below do) so the doctor verification gate sees it; the cwd-first precedence above applies to the running bot, not to `doctor`.

**Only two variables are required** — `config.py` raises `ValueError` and the bot exits with a help message if either is missing:

```bash
mkdir -p ~/.cc-telegram
cat > ~/.cc-telegram/.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:your_botfather_token_here
ALLOWED_USERS=123456789
EOF
chmod 600 ~/.cc-telegram/.env    # the file holds your bot token
```

- `TELEGRAM_BOT_TOKEN` — required. Read in `config.py`; scrubbed from `os.environ` after load (`SENSITIVE_ENV_VARS`) so child Claude panes never inherit it.
- `ALLOWED_USERS` — required. Comma-separated numeric Telegram user ids; also scrubbed after load.

### Recommended daily-driver `.env` (trusted machine only)

`--dangerously-skip-permissions` lets Claude act without local confirmation — only use it when `ALLOWED_USERS` is locked to you on a machine you trust.

```ini
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=<your_id>
CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
MONITOR_POLL_INTERVAL=1.0
OPENAI_API_KEY=sk-...               # voice transcription only
CC_TELEGRAM_BROWSE_ROOT=~/dev
# CC_TELEGRAM_VERBOSITY=standard
```

### Full env reference (all read in `config.py` unless noted)

| Var | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | — | BotFather token. Scrubbed after load. |
| `ALLOWED_USERS` | **yes** | — | Comma-separated Telegram user ids. Scrubbed after load. |
| `CC_TELEGRAM_DIR` | no | `~/.cc-telegram` | Config/state dir. Also read by `hook.py` and `_md_display_appender.py` (which never import `config`). Set in the launchd plist. |
| `TMUX_SESSION_NAME` | no | `cc-telegram` | tmux session the bot drives. |
| `CLAUDE_COMMAND` | no | `claude` | Launch command for new windows; may carry flags/env (e.g. `IS_SANDBOX=1 claude --dangerously-skip-permissions`). |
| `CLAUDE_CONFIG_DIR` | no | — | Claude config root; projects resolve to `$CLAUDE_CONFIG_DIR/projects`. |
| `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` | no | — | Explicit projects dir override. **Precedence: this > `CLAUDE_CONFIG_DIR/projects` > `~/.claude/projects`.** |
| `MONITOR_POLL_INTERVAL` | no | `2.0` | JSONL poll interval (seconds). |
| `OPENAI_API_KEY` | no | — | Voice transcription only. Scrubbed after load. Without it, voice notes fail with a 401. |
| `OPENAI_BASE_URL` | no | `https://api.openai.com/v1` | STT endpoint base. The transcription model is **hardcoded to `gpt-4o-transcribe`** (`transcribe.py`); the backend must expose that exact model name and no override env var exists. |
| `CC_TELEGRAM_BROWSE_ROOT` | no | `~` | Directory-browser starting root. |
| `CC_TELEGRAM_VERBOSITY` | no | `standard` | Default per-user output preset (`verbose`/`standard`/`compact`/`quiet`) for users with no stored `/settings` choice. |
| `CC_TELEGRAM_SHOW_USER_MESSAGES` | no | `true` | Echo user messages with 👤; explicit set becomes per-user default. |
| `CC_TELEGRAM_SHOW_TOOL_CALLS` | no | `true` | `false` suppresses tool-surface DISPLAY only (sidechains still feed run-state). |
| `CC_TELEGRAM_SHOW_HIDDEN_DIRS` | no | `false` | Show dot-dirs in the picker. |
| `CC_TELEGRAM_TOOL_SUMMARY_MAX_CHARS` | no | `40` | Tool-input chars in summary lines. |
| `CC_TELEGRAM_CONTEXT_PCT_THRESHOLD` | no | `80` | Context-% digest threshold. |
| `CC_TELEGRAM_CONTEXT_IN_MESSAGE_FOOTER` | no | `true` | Per-turn `📊 …` token footer. |
| `CC_TELEGRAM_AGENT_PROMPT_PREVIEW_CHARS` | no | `400` | 🤖 dispatch prompt excerpt length. |
| `CC_TELEGRAM_REPLY_CONTEXT` | no | `true` | Master kill-switch for reply/quote injection. |
| `CC_TELEGRAM_REPLY_CROSS_SESSION` | no | `true` | Cross-session quote rendering kill-switch (`config.py`). |
| `CC_TELEGRAM_QUOTE_INJECTION_MAX_CHARS` | no | `1600` | Quoted-excerpt cap. |
| `CC_TELEGRAM_AGGREGATOR_DEBOUNCE_SECONDS` | no | `1.5` | Media/caption coalescing window. |
| `CC_TELEGRAM_AGGREGATOR_MAX_ATTACHMENTS` | no | `10` | Per-bundle attachment cap. |
| `CC_TELEGRAM_MAX_ATTACHMENT_SIZE_BYTES` | no | `20971520` | Document download cap (20 MB). |
| `CC_TELEGRAM_MESSAGE_REFS_DB_PATH` | no | `$CC_TELEGRAM_DIR/message_refs.db` | SQLite provenance index path. |
| `CC_TELEGRAM_MESSAGE_REFS_RETENTION_DAYS` | no | `30` | Provenance GC retention. |
| `CC_TELEGRAM_MESSAGE_REF_TEXT_MAX_CHARS` | no | `4000` | Stored text-column cap. |
| `CC_TELEGRAM_LOG_ROTATE_THRESHOLD_MB` | no | `50` | Log-rotate threshold. Read ONLY by `bin/rotate-logs.sh` (not the Python app); set via the log-rotate plist. |
| `CC_TELEGRAM_LOG_ROTATE_MAX_AGE_DAYS` | no | `14` | Log-archive retention. Same — `bin/rotate-logs.sh` only. |

---

## 5. Install the Claude Code hooks

```bash
cc-telegram hook --install          # (dev: uv run cc-telegram hook --install)
grep -c 'cc-telegram hook' ~/.claude/settings.json    # expect 3
```

This idempotently installs/refreshes **three managed hook entries** in `~/.claude/settings.json` (`hook.py:_install_hook`):

1. **`SessionStart`** (timeout 5s) — writes `$CC_TELEGRAM_DIR/session_map.json` (key `tmux_session:window_id` → `{session_id, cwd, window_name}`) so the bot routes Claude's output back to the right topic, and `/clear` / resumed sessions stay attached.
2. **`PreToolUse`** matcher `AskUserQuestion` (timeout 2s) — captures the structured question payload (option descriptions) to `auq_pending/<session_id>.json` *before* the picker renders, so the Telegram picker shows full option descriptions at first render.
3. **`Notification`** matcher-less (timeout 2s) — writes a window-keyed `notify_pending/<session_id>.json` marker (`{ts, window_key, generation, kind}`, **no message text**) when Claude blocks on a permission/approval gate (including the Workflow tool's Bash-approval gate, which leaves no JSONL trace), driving the "🔔 Waiting on you" state.

All three run the command `cc-telegram hook` (resolved to the installed binary's absolute path). The bot logs a one-time startup warning if `PreToolUse` or `Notification` is missing; re-run `cc-telegram hook --install` to repair.

> **A fourth hook — `MessageDisplay` (live prose) — needs NO manual install.** The bot writes its own `$CC_TELEGRAM_DIR/md_hook_settings.json` and injects it per-window via `claude --settings`, so it is scoped to the bot's windows and never written into the global `~/.claude/settings.json`.

> **Note:** `cc-telegram doctor` only verifies the `SessionStart` hook (see `doctor.py`). Use the `grep -c` above to confirm all three were installed.

---

## 6. Verify the install, then run

### Doctor

```bash
cc-telegram doctor    # (dev: uv run cc-telegram doctor)
```

`doctor.py` checks: `TELEGRAM_BOT_TOKEN` present, `ALLOWED_USERS` present, `tmux` on PATH, `claude` on PATH, the **SessionStart** hook installed, and the config dir writable. Exit 0 only when zero FAILs. It reads the already-exported environment plus the config-dir `$CC_TELEGRAM_DIR/.env` directly (**not** a cwd `./.env`), so it works before the bot runs — keep your `.env` in `$CC_TELEGRAM_DIR` so this gate sees it. **It does NOT check the PreToolUse/Notification hooks** — confirm those with the section 5 `grep -c`.

### Foreground smoke test

```bash
cc-telegram             # (dev: uv run cc-telegram)
```

`main._run_bot` loads config (exits with a help message if the two required vars are missing), creates/attaches the tmux session via `tmux_manager.get_or_create_session()`, then `run_polling(allowed_updates=["message","callback_query"])`. You should see `Tmux session 'cc-telegram' ready` and `Starting Telegram bot...`. Ctrl-C to stop.

---

## 7. Run under launchd (macOS daemon)

There is **no main-bot plist in the repo** (only the log-rotate scripts in `bin/`). The repo ships **`bin/install-service.sh`** to generate and load the LaunchAgent for you (label **`com.cc-telegram`**), or you can write the plist by hand. Both produce the same agent.

### Easiest — the install script

```bash
bash bin/install-service.sh          # templates the plist with $HOME / the installed cc-telegram path, then bootstrap + enable
bash bin/install-service.sh --print  # dry-run: print the plist it WOULD write, do nothing (still needs cc-telegram on PATH)
```

The script resolves the installed `cc-telegram` (must be on PATH first — section 3 Mode A), writes `~/Library/LaunchAgents/com.cc-telegram.plist`, and `launchctl bootstrap`/`enable`s it. It is idempotent (re-running boots out and re-loads).

### Or by hand

> **Why an explicit `PATH` in the plist:** launchd's default `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin`, which would NOT find `cc-telegram`, `tmux`, or `claude`. Put `~/.local/bin` and your homebrew bin on it.

```bash
cat > ~/Library/LaunchAgents/com.cc-telegram.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.cc-telegram</string>
  <key>ProgramArguments</key>
  <array><string>$HOME/.local/bin/cc-telegram</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>CC_TELEGRAM_DIR</key><string>$HOME/.cc-telegram</string>
  </dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>StandardOutPath</key><string>$HOME/.cc-telegram/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/.cc-telegram/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.cc-telegram.plist
launchctl enable "gui/$(id -u)/com.cc-telegram"
launchctl print "gui/$(id -u)/com.cc-telegram" | grep -E 'state|pid'    # expect state = running
```

`KeepAlive=true` + `RunAtLoad=true` means launchd restarts the bot on crash and at login. stdout/stderr go to `$CC_TELEGRAM_DIR/launchd.{out,err}.log` (these files exist ONLY because of this plist's redirect; the bot's own logging goes to stderr → `launchd.err.log`).

### (Optional) log rotation

launchd-redirected logs are not rotated by Python. A crash-loop can balloon `launchd.err.log`. Install the rotation agent (this one IS in the repo):

```bash
bash bin/install-log-rotate.sh
```

This writes `~/Library/LaunchAgents/com.cc-telegram.log-rotate.plist` (label `com.cc-telegram.log-rotate`, `StartInterval` 1800s), copies `bin/rotate-logs.sh` into `$CC_TELEGRAM_DIR/rotate-logs.sh`, and gzips logs over 50 MB into `$CC_TELEGRAM_DIR/log-archive/` (prunes >14 days). Force a pass: `launchctl kickstart gui/$(id -u)/com.cc-telegram.log-rotate`.

---

## 8. Deploy / upgrade after code changes (the canonical recipe)

Once the LaunchAgent exists, the steady-state redeploy is:

```bash
# 1. Reinstall the binary — --no-cache is MANDATORY (uv's cache is version-keyed and
#    the version is not bumped on every deploy, so --force alone reuses a stale wheel)
uv tool install --force --no-cache .

# 2. (Strongly recommended) prove your change is actually in the installed binary
grep -rl '<a-unique-symbol-from-your-diff>' ~/.local/share/uv/tools/cc-telegram/

# 3. Kill + restart the running service
launchctl kickstart -k gui/$(id -u)/com.cc-telegram
```

**Why step 1 needs `--no-cache`:** uv's wheel cache is keyed on the version string, which does not change on most deploys, so `--force` alone reinstalls the cached old wheel and your fix silently never ships (exit 0, no error). Always include `--no-cache`. Step 2 is the cheap insurance that catches a cache miss before you waste a debug cycle.

**Deploy reach (no per-topic action needed):** all bot-side logic (parser, interactive_ui, status_polling, route_runtime, monitor, queue) goes live to every bound topic on the next poll after kickstart. The ONLY exception is the launch-injected `MessageDisplay --settings` hook: a session that predates that hook needs a fresh window (relaunch the topic's session) to pick it up.

---

## 9. Create and bind your first topic

The bot is topic-only (1 Topic = 1 tmux window = 1 Claude session).

1. In your forum-enabled supergroup, create a **new Topic** and send any message in it.
2. Because the topic is unbound, the bot replies with a **directory browser** — pick a working directory.
3. If that dir has existing Claude sessions you get a **session picker** (resume via `claude --resume <id>`); otherwise the bot creates a new tmux window running `CLAUDE_COMMAND`, binds the topic to that window id (in `state.json` `thread_bindings`), and forwards your pending message.
4. From then on, every message in that topic streams to that Claude session, and Claude's output / interactive prompts / status return to the topic.

Useful Telegram commands: `/history`, `/screenshot`, `/esc`, `/settings`, `/dashboard [pin]`, `/kill`, `/unbind`, `/usage`. Forwarded Claude commands: `/clear`, `/compact`, `/cost`, `/model`, `/effort`. Closing/deleting the topic kills the tmux window and unbinds it.

---

## 10. Verify it's working

```bash
# launchd state + pid
launchctl print "gui/$(id -u)/com.cc-telegram" | grep -E 'state|pid'

# tail the live log — look for "Tmux session 'cc-telegram' ready" + "Starting Telegram bot..."
tail -f ~/.cc-telegram/launchd.err.log    # logging goes to stderr; this is the main signal
tail -f ~/.cc-telegram/launchd.out.log

# confirm the tmux session exists
tmux ls | grep cc-telegram

# confirm the hooks (expect 3)
grep -c 'cc-telegram hook' ~/.claude/settings.json

# confirm a topic bound (after step 9)
python3 -m json.tool ~/.cc-telegram/state.json | grep -A3 thread_bindings
```

End-to-end smoke: bind a topic (section 9), send "say hi", confirm Claude's reply lands in the topic.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **Deployed a fix, behavior unchanged, no error** | `uv tool install` reused the cached wheel (you omitted `--no-cache`) | Re-run `uv tool install --force --no-cache .`; `grep` the installed binary under `~/.local/share/uv/tools/cc-telegram/` for a unique new symbol BEFORE kickstart |
| **`launchctl kickstart ... com.cc-telegram` → "No such process / Could not find service"** | The main-bot LaunchAgent was never created (it is NOT in the repo) | Create it per section 7 (`bash bin/install-service.sh`, or by hand), then `launchctl bootstrap`/`enable` |
| **Topic shows opaque/empty output; tmux `claude` errors immediately** | Claude Code is unauthenticated (cc-telegram manages no Anthropic creds) | Run `claude` once interactively to log in, or export `ANTHROPIC_API_KEY` into the bot's launch env (e.g. add it to the plist `EnvironmentVariables`) |
| **`cc-telegram: command not found`** | You ran `uv sync` (dev mode, not on PATH) but used a bare command | Use `uv run cc-telegram ...`, or install as a tool (section 3 Mode A) and ensure `~/.local/bin` is on PATH |
| **Bot exits at startup with "TELEGRAM_BOT_TOKEN/ALLOWED_USERS required"** | Missing required env | Populate `~/.cc-telegram/.env` (section 4); `cc-telegram doctor` to confirm |
| **AUQ option descriptions missing / "🔔 Waiting on you" never appears** | PreToolUse and/or Notification hook not installed (doctor won't flag these) | `cc-telegram hook --install`; verify `grep -c 'cc-telegram hook' ~/.claude/settings.json` == 3; check the bot startup-log warning |
| **Voice note → opaque 401** | `OPENAI_API_KEY` unset (required for voice only) | Set `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` if non-OpenAI) |
| **Voice note → model-not-found** | Backend lacks the hardcoded `gpt-4o-transcribe` model (e.g. an OpenRouter/whisper-1 backend) | Point `OPENAI_BASE_URL` at a backend exposing `gpt-4o-transcribe`, or front it with a model-name-translating proxy |
| **Interactive AUQ taps stop working after a Claude Code update** | The TUI parser is **version-sensitive** (dispatch was validated against v2.1.168) | Capture fresh terminal fixtures for the new version and re-test before relying on AUQ dispatch |
| **`launchd.err.log` ballooning** | Crash-loop under `KeepAlive=true` | Fix the startup error in the log; install log rotation (`bash bin/install-log-rotate.sh`) to cap the blast radius |
| **Stale window bindings after a tmux server restart** | Window ids reset on restart | The bot re-resolves persisted display names to live window ids on startup (`resolve_stale_ids`); restart the bot |

All state files under `$CC_TELEGRAM_DIR` are safe to delete — the bot re-creates what it needs (you lose interactive picker continuity and bound-topic mappings). See the README "State files" section for the full inventory.
