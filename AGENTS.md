# AGENTS.md

## What this is

**cc-telegram** is a Telegram bot that bridges Telegram Forum topics to Claude Code sessions running in tmux windows: **1 Topic = 1 tmux window = 1 Claude Code session**, all keyed internally by tmux window id (`@0`, `@12`), never by window name. The terminal is the source of truth; Telegram is the remote control + notification layer. Tech: Python 3.12+, python-telegram-bot, tmux (via libtmux), `uv`.

## Canonical docs (read these before changing anything)

- **`README.md`** — features, env vars, state files, hook config.
- **`docs/DEPLOYMENT.md`** — end-to-end setup, the deploy/upgrade recipe, troubleshooting.
- **`CLAUDE.md`** — build/lint/type/test commands, core design constraints, the README-sync rule.
- **`.claude/rules/`** — `architecture.md` (system diagram + module inventory), `topic-architecture.md` (topic→window→session mapping), `message-handling.md` (queue, run-state, interactive UI). These are dense and load-bearing; the RouteRuntime / interactive-UI invariants live here.

## Golden path

```bash
# Develop from source
uv sync --all-extras
uv run cc-telegram doctor
uv run cc-telegram                       # run the bot in the foreground

# Gates (MUST pass before committing — see CLAUDE.md)
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pyright src/cctelegram/           # 0 errors required
uv run pytest -m scenario -q             # black-box behavior floor
uv run pytest --tb=short -q

# Deploy / upgrade (macOS launchd; the running service is label `com.cc-telegram`)
uv tool install --force --no-cache .     # --no-cache is MANDATORY (see gotcha #1)
launchctl kickstart -k gui/$(id -u)/com.cc-telegram
```

## Top gotchas

1. **`--no-cache` is mandatory on every deploy.** uv's wheel cache is keyed on the package version, and the version is not bumped on every code change. So `uv tool install --force .` *without* `--no-cache` silently reinstalls the stale cached wheel — your code never ships and there is NO error. Always `uv tool install --force --no-cache .`, and grep the installed binary under `~/.local/share/uv/tools/cc-telegram/` for a unique symbol before kickstart.
2. **No main-bot plist is in the repo.** Use `bash bin/install-service.sh` (or hand-write the plist per `docs/DEPLOYMENT.md` section 7). The `launchctl kickstart ... com.cc-telegram` recipe assumes that LaunchAgent at `~/Library/LaunchAgents/com.cc-telegram.plist` exists; it does not on a fresh machine.
3. **Topic-only.** No DM/General routing, no `active_sessions`, no `/list`, no non-topic back-compat. Every path assumes named forum topics keyed by tmux window id.
4. **The TUI parser is version-sensitive.** Interactive UI (AskUserQuestion / ExitPlanMode / Permission) detection and pick dispatch are parsed from the live tmux pane; behavior is validated against a specific Claude Code version (currently v2.1.168). A Claude Code update can silently break detection/dispatch — capture fresh fixtures and re-test before trusting it.
5. **cc-telegram manages no Anthropic credentials.** It shells out to the `claude` CLI per window; Claude Code must be independently authenticated, or topics show opaque failures. The bot scrubs only `TELEGRAM_BOT_TOKEN`/`ALLOWED_USERS`/`OPENAI_API_KEY` from child env.

## Repo conventions

- Every `.py` starts with a module docstring (one-sentence summary first line).
- Pull-only everywhere — there is **no** observer/push channel (the `route_runtime` design forbids the `register_*_callback` fan-out that caused bug c313657).
- README-sync rule (CLAUDE.md): any change adding a hook / env var / state file / external config dep MUST update `README.md` (and `.claude/rules/architecture.md` for architecture changes) in the same PR. Stale README is a P2 review finding.
