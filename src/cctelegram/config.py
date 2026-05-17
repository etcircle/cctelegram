"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CC_TELEGRAM_DIR/.env (default ~/.cc-telegram).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import app_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux)
SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "OPENAI_API_KEY"}


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = app_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "cc-telegram")
        self.tmux_main_window_name = "__main__"

        # Claude command to run in new windows
        self.claude_command = os.getenv("CLAUDE_COMMAND", "claude")

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Claude Code session monitoring configuration
        # Support custom projects path for Claude variants (e.g., cc-mirror, zai)
        # Priority: CC_TELEGRAM_CLAUDE_PROJECTS_PATH > CLAUDE_CONFIG_DIR/projects > default
        custom_projects_path = os.getenv("CC_TELEGRAM_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")

        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = (
            os.getenv("CC_TELEGRAM_SHOW_USER_MESSAGES", "true").lower() != "false"
        )

        # Show tool call notifications (tool_use/tool_result) in Telegram
        # When False, only text responses, thinking, and interactive prompts are sent
        self.show_tool_calls = (
            os.getenv("CC_TELEGRAM_SHOW_TOOL_CALLS", "true").lower() != "false"
        )

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CC_TELEGRAM_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # Max length of the per-tool input string surfaced in tool_use summary
        # lines (e.g. "**Bash**(<command>)", "**Read**(<path>)"). Long inputs
        # are truncated with a "…" marker. Default 40 keeps the activity feed
        # compact; raise it (e.g. 600) to preserve full bash one-liners at the
        # cost of multi-line summary entries on Telegram.
        try:
            self.tool_summary_max_chars = int(
                os.getenv("CC_TELEGRAM_TOOL_SUMMARY_MAX_CHARS", "40")
            )
        except ValueError:
            self.tool_summary_max_chars = 40

        # Stage-3 event-driven RunState. Gates two coupled changes together:
        #   1. bot.py: whether monitor.set_event_callback is wired
        #   2. activity-digest renderer + status_polling: whether they read
        #      RunState / context_pct, or fall back to the legacy
        #      state.done / is_status_active(pane) paths.
        # Default true after Stage 4 + the lifecycle-event / pane-idle
        # backstop fixes landed; legacy V1 path remains accessible by
        # setting CC_TELEGRAM_BUSY_INDICATOR_V2=false.
        self.busy_indicator_v2 = (
            os.getenv("CC_TELEGRAM_BUSY_INDICATOR_V2", "true").lower() == "true"
        )

        # Context-window indicator threshold (percent). The activity-digest
        # header appends "· ctx NN%" when the cached value crosses this; at
        # ≥95 it prepends a warning glyph. Below threshold or unknown: no
        # suffix. Pure visual policy — does not affect RunState.
        try:
            self.context_pct_threshold = int(
                os.getenv("CC_TELEGRAM_CONTEXT_PCT_THRESHOLD", "80")
            )
        except ValueError:
            self.context_pct_threshold = 80

        # Per-turn footer in assistant messages, e.g. "📊 113k / 200k".
        # Snapshot at send-time on end-of-turn text bubbles only — no edits,
        # so MarkdownV2 is rendered once and forgotten.
        self.context_in_message_footer = (
            os.getenv("CC_TELEGRAM_CONTEXT_IN_MESSAGE_FOOTER", "true").lower() == "true"
        )

        # §2.7 Agent (subagent) prompt excerpt length for the top-level
        # "🤖 Subagent dispatched" message. Long prompts get truncated mid-line
        # with a "…" marker; the full prompt is still in the JSONL transcript.
        try:
            self.agent_prompt_preview_chars = int(
                os.getenv("CC_TELEGRAM_AGENT_PROMPT_PREVIEW_CHARS", "400")
            )
        except ValueError:
            self.agent_prompt_preview_chars = 400

        # §2.5 Telegram reply-context bridge.
        # Master kill-switch: when False, ``text_handler`` skips
        # ``extract_reply_context`` and outbound sends drop ``reply_parameters``
        # entirely. Lets us roll back the bridge without redeploying.
        self.reply_context_enabled = (
            os.getenv("CC_TELEGRAM_REPLY_CONTEXT", "true").lower() != "false"
        )

        # P1.5: when True (default), a reply quoting a message from a
        # previous Claude session renders an annotated cross-session marker
        # into the prompt instead of silently dropping the quote. Set
        # ``CC_TELEGRAM_REPLY_CROSS_SESSION=false`` to revert to the
        # pre-P1.5 silent-drop behaviour without redeploying.
        self.reply_context_cross_session_enabled = (
            os.getenv("CC_TELEGRAM_REPLY_CROSS_SESSION", "true").lower() != "false"
        )

        # Upper bound on the quoted-text excerpt injected into Claude's
        # prompt. The full original text still lives in the Telegram message,
        # and Stage 5.c will keep a SQLite copy for rehydration; this cap just
        # keeps the per-turn injection proportional to the new user text.
        try:
            self.quote_injection_max_chars = int(
                os.getenv("CC_TELEGRAM_QUOTE_INJECTION_MAX_CHARS", "1600")
            )
        except ValueError:
            self.quote_injection_max_chars = 1600

        # §2.8 Inbound aggregator (caption + media-group + photo+text bundling).
        # Debounce window for coalescing Telegram messages into a single
        # Claude turn. Mirrors the debounce Telegram clients use to bundle
        # media-group uploads.
        try:
            self.aggregator_debounce_seconds = float(
                os.getenv("CC_TELEGRAM_AGGREGATOR_DEBOUNCE_SECONDS", "1.5")
            )
        except ValueError:
            self.aggregator_debounce_seconds = 1.5

        # Hard cap on attachments per aggregated bundle. Beyond this, the
        # aggregator force-flushes immediately rather than waiting on the
        # debounce — prevents an unbounded media dump from blocking flush.
        raw_max_attachments = os.getenv("CC_TELEGRAM_AGGREGATOR_MAX_ATTACHMENTS")
        try:
            self.aggregator_max_attachments = (
                int(raw_max_attachments) if raw_max_attachments else 10
            )
        except ValueError:
            self.aggregator_max_attachments = 10

        # Upper bound on document uploads forwarded to Claude Code. Telegram
        # itself caps bot getFile downloads at 20 MB; self-hosted Bot API
        # users can raise this. We fail fast before download to avoid the
        # less-actionable error from get_file() past the cap.
        try:
            self.max_attachment_size_bytes: int = int(
                os.getenv(
                    "CC_TELEGRAM_MAX_ATTACHMENT_SIZE_BYTES", str(20 * 1024 * 1024)
                )
            )
        except ValueError:
            self.max_attachment_size_bytes = 20 * 1024 * 1024

        # §2.5.3 Stage 5.c: Telegram message-refs SQLite. The DB file lives
        # under the config dir by default so it follows the rest of CC Telegram's
        # state files; CC_TELEGRAM_MESSAGE_REFS_DB_PATH overrides for tests / split
        # storage volumes.
        message_refs_path = os.getenv("CC_TELEGRAM_MESSAGE_REFS_DB_PATH")
        if message_refs_path:
            self.message_refs_db_path = Path(message_refs_path)
        else:
            self.message_refs_db_path = self.config_dir / "message_refs.db"

        # Daily GC retention for the provenance table (§2.5.3). Older rows
        # still resolve via Telegram's UI quote bubble; Claude just won't get
        # transcript provenance for them.
        try:
            self.message_refs_retention_days = int(
                os.getenv("CC_TELEGRAM_MESSAGE_REFS_RETENTION_DAYS", "30")
            )
        except ValueError:
            self.message_refs_retention_days = 30

        # Bound on the ``text`` column in ``telegram_message_refs``. Long
        # bodies are truncated with a ``… [truncated]`` marker; the sha256
        # column still hashes the full original text for verification.
        try:
            self.message_ref_text_max_chars = int(
                os.getenv("CC_TELEGRAM_MESSAGE_REF_TEXT_MAX_CHARS", "4000")
            )
        except ValueError:
            self.message_ref_text_max_chars = 4000

        # Directory-browser starting point. Defaults to ``~`` so the picker
        # opens in the user's home regardless of where the bot was launched
        # from. Previously defaulted to Path.cwd(), which surfaced CC Telegram's
        # own repo when restarted from inside the project tree — surprising
        # and OS-coupled. Override with CC_TELEGRAM_BROWSE_ROOT to pin a specific
        # workspace directory.
        browse_root_env = os.getenv("CC_TELEGRAM_BROWSE_ROOT")
        self.browse_root: Path = (
            Path(browse_root_env).expanduser().resolve()
            if browse_root_env
            else Path.home()
        )

        # OpenAI API for voice message transcription (optional)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        # Scrub sensitive vars from os.environ so child processes never inherit them.
        # Values are already captured in Config attributes above.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.claude_projects_path,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
