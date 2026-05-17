"""Application entry point — CLI dispatcher and bot bootstrap.

Provides the public `cc-telegram` command with three modes:
  1. no subcommand — start the Telegram polling bot;
  2. `hook` — process/install the Claude Code SessionStart hook;
  3. `doctor` — report or perform one-shot state migration.
"""

import argparse
import logging
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-telegram",
        description="Telegram bridge for Claude Code sessions.",
    )
    subparsers = parser.add_subparsers(dest="command")

    hook = subparsers.add_parser(
        "hook",
        help="Process or install the Claude Code SessionStart hook.",
    )
    hook.add_argument(
        "--install",
        action="store_true",
        help="Install or rewrite the SessionStart hook in ~/.claude/settings.json.",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="Check or migrate CC Telegram state/config directory.",
    )
    doctor.add_argument(
        "--migrate",
        action="store_true",
        help="Copy ~/.ccbot contents into ~/.cc-telegram when needed.",
    )
    return parser


def _run_bot() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    from .doctor import preflight_or_exit

    preflight_or_exit()

    try:
        from .config import config
    except ValueError as e:
        from .utils import app_dir

        env_path = app_dir() / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("cctelegram").setLevel(logging.DEBUG)
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .bot import create_bot
    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    application = create_bot()
    application.run_polling(allowed_updates=["message", "callback_query"])


def main(argv: list[str] | None = None) -> None:
    """Main entry point for the cc-telegram console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "hook":
        from .hook import hook_main

        raise SystemExit(hook_main(["--install"] if args.install else []))
    if args.command == "doctor":
        from .doctor import doctor_main

        doctor_args = ["--migrate"] if args.migrate else []
        raise SystemExit(doctor_main(doctor_args))

    _run_bot()


if __name__ == "__main__":
    main()
