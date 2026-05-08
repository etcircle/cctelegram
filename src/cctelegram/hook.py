"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window↔session mapping
in <CCTELEGRAM_DIR>/session_map.json. Also provides `--install` to configure
or rewrite the hook in ~/.claude/settings.json.

This module must not import config.py: hooks run inside tmux panes where bot
env vars are not guaranteed to exist. Config directory resolution uses
utils.app_dir(), which only needs CCTELEGRAM_DIR.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
_HOOK_COMMAND_SUFFIX = "cctelegram hook"
HookStatus = Literal["current", "missing"]


def _command_matches(cmd: str, suffix: str) -> bool:
    return cmd == suffix or cmd.endswith("/" + suffix)


def _find_cctelegram_path() -> str:
    """Find the executable used in the SessionStart hook command."""
    bin_path = shutil.which("cctelegram")
    if bin_path:
        return bin_path

    venv_bin = Path(sys.executable).parent / "cctelegram"
    if venv_bin.exists():
        return str(venv_bin)

    return "cctelegram"


def _is_hook_installed(settings: dict) -> HookStatus:
    """Return whether the settings contain the canonical CCTelegram hook."""
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if _command_matches(cmd, _HOOK_COMMAND_SUFFIX):
                return "current"
    return "missing"


def _install_hook(settings_file: Path = _CLAUDE_SETTINGS_FILE) -> int:
    """Install or rewrite the CCTelegram hook in Claude settings.json."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    hook_command = f"{_find_cctelegram_path()} hook"
    status = _is_hook_installed(settings)
    if status == "current":
        logger.info("Hook already installed in %s", settings_file)
        print(f"Hook already installed in {settings_file}")
        return 0

    hook_config = {"type": "command", "command": hook_command, "timeout": 5}
    settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(
        {"hooks": [hook_config]}
    )
    logger.info("Installing hook command: %s", hook_command)
    action = f"Hook installed successfully in {settings_file}"

    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    print(action)
    return 0


def hook_main(argv: list[str] | None = None) -> int:
    """Process a Claude Code hook event from stdin, or install the hook."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="cctelegram hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into ~/.claude/settings.json",
    )
    if argv is None:
        argv = sys.argv[2:]
    args, _ = parser.parse_known_args(argv)

    if args.install:
        logger.info("Hook install requested")
        return _install_hook()

    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return 0

    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return 0

    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return 0

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return 0

    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if entrypoint.startswith("sdk-"):
        source = payload.get("source", "")
        logger.info(
            "Skipping SDK sub-agent hook (entrypoint=%s, source=%s, sid=%s)",
            entrypoint,
            source,
            session_id,
        )
        return 0

    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return 0

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return 0
    tmux_session_name, window_id, window_name = parts
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    from .utils import app_dir

    map_file = app_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                }

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
    return 0
