"""Hook subcommand for Claude Code session tracking + AUQ description capture.

Called by Claude Code's hook system to:
  1. SessionStart: maintain a window↔session mapping in
     <CC_TELEGRAM_DIR>/session_map.json. Same as v1.
  2. PreToolUse (matcher=AskUserQuestion): capture the structured
     tool_input (questions + per-option descriptions) BEFORE Claude
     Code's TUI renders the picker, and write it to
     <CC_TELEGRAM_DIR>/auq_pending/<session_id>.json so the bot's
     pretool reader (handlers/interactive_ui.py) can post the AUQ
     context message with descriptions at first render instead of
     just labels.

The hook is a pure observer for PreToolUse — exit 0 with no stdout, no
permission decision. Exceptions are swallowed and logged; the tool call
must NEVER be blocked by hook bugs.

`--install` installs (or refreshes) both hook entries in
~/.claude/settings.json idempotently. SessionStart keeps a 5s timeout
(existing); PreToolUse uses 2s (write is sub-ms; observer hooks should
not delay tool execution beyond a hard ceiling).

This module must not import config.py: hooks run inside tmux panes where
bot env vars are not guaranteed to exist. Config directory resolution
uses utils.app_dir(), which only needs CC_TELEGRAM_DIR.
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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
_CURRENT_HOOK_COMMAND_SUFFIX = "cc-telegram hook"
_HOOK_PATH_PREFIX_RE = re.compile(r"^[A-Za-z0-9_./~@+-]+$")
_AUQ_MATCHER = "AskUserQuestion"
_SESSION_START_TIMEOUT_S = 5
_PRE_TOOL_USE_TIMEOUT_S = 2
HookStatus = Literal["current", "missing"]


def _command_matches(cmd: str, suffix: str) -> bool:
    """Return whether cmd is exactly suffix or a bare path ending in suffix.

    Claude hook commands are shell command strings, but this installer only
    owns the simple commands it writes itself: ``cc-telegram hook`` and
    path-qualified variants such as ``/opt/bin/cc-telegram hook``. Requiring
    the path prefix to be the whole command, and to contain only ordinary path
    characters, prevents wrapper/comment strings like
    ``echo /opt/bin/cc-telegram hook`` from being treated as managed hooks.
    """
    if cmd == suffix:
        return True

    path_suffix = "/" + suffix
    if not cmd.endswith(path_suffix):
        return False

    path_prefix = cmd[: -len(path_suffix)]
    return _HOOK_PATH_PREFIX_RE.fullmatch(path_prefix) is not None


def _find_cc_telegram_path() -> str:
    """Find the executable used in hook commands."""
    bin_path = shutil.which("cc-telegram")
    if bin_path:
        return bin_path

    venv_bin = Path(sys.executable).parent / "cc-telegram"
    if venv_bin.exists():
        return str(venv_bin)

    return "cc-telegram"


def _entry_has_managed_command(entry: dict) -> bool:
    """Return True if a settings.json hook-list entry contains a managed
    ``cc-telegram hook`` command."""
    inner_hooks = entry.get("hooks", [])
    if not isinstance(inner_hooks, list):
        return False
    for h in inner_hooks:
        if not isinstance(h, dict):
            continue
        cmd = h.get("command", "")
        if not isinstance(cmd, str):
            continue
        if _command_matches(cmd, _CURRENT_HOOK_COMMAND_SUFFIX):
            return True
    return False


def _is_session_start_installed(settings: dict) -> HookStatus:
    """Return whether SessionStart contains the managed hook command."""
    for entry in settings.get("hooks", {}).get("SessionStart", []) or []:
        if isinstance(entry, dict) and _entry_has_managed_command(entry):
            return "current"
    return "missing"


def _is_pre_tool_use_installed(settings: dict) -> HookStatus:
    """Return whether PreToolUse contains a managed entry matching
    ``AskUserQuestion``.

    The check is intentionally loose: any managed-command entry under
    matcher ``AskUserQuestion`` counts as ``current``, regardless of
    ``type`` / ``timeout``. This preserves idempotency — install never
    duplicates an existing managed entry — at the cost of NOT
    auto-refreshing a stale timeout config. Codex P2 round 2 flagged
    this; the trade-off favors idempotency since the install command is
    append-only and an in-place replacement would risk clobbering
    user-edited entries. Users who need a config refresh can edit
    ~/.claude/settings.json directly.
    """
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("matcher") != _AUQ_MATCHER:
            continue
        if _entry_has_managed_command(entry):
            return "current"
    return "missing"


def _install_hook(settings_file: Path = _CLAUDE_SETTINGS_FILE) -> int:
    """Install or refresh the CC Telegram hooks in Claude settings.json.

    Two events are managed:
      - SessionStart with timeout 5 (window↔session map)
      - PreToolUse with matcher AskUserQuestion and timeout 2 (AUQ
        descriptions capture)

    Idempotent: missing entries are added, current ones left alone.
    """
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    hook_cmd = f"{_find_cc_telegram_path()} hook"
    session_start_cfg = {
        "type": "command",
        "command": hook_cmd,
        "timeout": _SESSION_START_TIMEOUT_S,
    }
    pre_tool_use_cfg = {
        "type": "command",
        "command": hook_cmd,
        "timeout": _PRE_TOOL_USE_TIMEOUT_S,
    }
    installed: list[str] = []

    if _is_session_start_installed(settings) == "missing":
        settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(
            {"hooks": [session_start_cfg]}
        )
        installed.append("SessionStart")

    if _is_pre_tool_use_installed(settings) == "missing":
        settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
            {"matcher": _AUQ_MATCHER, "hooks": [pre_tool_use_cfg]}
        )
        installed.append(f"PreToolUse({_AUQ_MATCHER})")

    if not installed:
        logger.info("All hooks already current in %s", settings_file)
        print(f"All hooks already current in {settings_file}")
        return 0

    logger.info("Installing hook entries: %s", ", ".join(installed))
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    print(f"Installed in {settings_file}: {', '.join(installed)}")
    return 0


def _resolve_tmux_window_key(
    pane_id: str,
) -> tuple[str, str, str] | None:
    """Return (tmux_session_name, window_id, window_name) for ``pane_id``,
    or None if tmux can't resolve it."""
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
    raw = result.stdout.strip()
    parts = raw.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw,
        )
        return None
    return parts[0], parts[1], parts[2]


def _handle_session_start(payload: dict) -> int:
    """Write the window↔session mapping to session_map.json.

    Same behavior as the v1 hook. Kept in its own function so the
    PreToolUse handler can share validation / SDK-skip logic via
    ``hook_main``'s dispatcher.
    """
    session_id = payload["session_id"]  # validated by caller
    cwd = payload.get("cwd", "")

    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return 0

    resolved = _resolve_tmux_window_key(pane_id)
    if resolved is None:
        return 0
    tmux_session_name, window_id, window_name = resolved
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    from .utils import app_dir, atomic_write_json

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


def _handle_pre_tool_use(payload: dict) -> int:
    """Capture AskUserQuestion tool_input to a per-session side file.

    Fires BEFORE Claude Code renders the picker UI (PreToolUse semantics).
    The hook is purely observational — exit 0 with no stdout, no
    permission decision, no exceptions propagated. Hook bugs MUST NEVER
    block Claude Code's tool execution.

    Side file path: <CC_TELEGRAM_DIR>/auq_pending/<session_id>.json
    Schema version 1. The reader is in handlers/interactive_ui.py
    (next chunk in the wave; not present yet — this commit writes the
    file even though nobody reads it, which is intentional staging).
    """
    tool_name = payload.get("tool_name", "")
    if tool_name != _AUQ_MATCHER:
        # Matcher should have filtered already; defensive check.
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        logger.warning(
            "PreToolUse AUQ: invalid tool_input shape (%s)", type(tool_input).__name__
        )
        return 0
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        logger.warning("PreToolUse AUQ: missing questions array")
        return 0

    from .terminal_parser import (
        questions_content_digest,
        questions_content_pairs_from_tool_input,
    )
    from .utils import app_dir, atomic_write_json

    pairs = questions_content_pairs_from_tool_input(tool_input)
    if pairs is None:
        logger.warning("PreToolUse AUQ: tool_input failed shape validation")
        return 0
    fingerprint = questions_content_digest(pairs)

    session_id = payload["session_id"]  # validated by caller
    pending_dir = app_dir() / "auq_pending"

    # Reject symlinks anywhere on the path — defensive against attempts
    # to redirect writes to arbitrary files via a hostile symlink in
    # ~/.cc-telegram/. We OWN this directory and never want it pointing
    # anywhere unexpected.
    if pending_dir.exists() and pending_dir.is_symlink():
        logger.error("PreToolUse AUQ: %s is a symlink; refusing to write", pending_dir)
        return 0

    try:
        pending_dir.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        logger.error("PreToolUse AUQ: mkdir %s failed: %s", pending_dir, e)
        return 0

    # mkdir(mode=...) only applies to newly created dirs; chmod
    # unconditionally to recover from a pre-existing loose-mode dir.
    # Fail-closed (codex P2 round 2 — chunk 2): if we can't tighten the
    # dir to 0o700, we MUST NOT write the side file. AUQ tool_input can
    # carry sensitive context (skill prompts that mention infra, plan
    # decisions, etc.); a world-readable side file is a privacy
    # regression, and the existing post-hoc form-source fallback gives
    # the user a working — if less rich — experience either way.
    try:
        os.chmod(pending_dir, 0o700)
    except OSError as e:
        logger.error(
            "PreToolUse AUQ: chmod 0700 on %s failed; refusing to write: %s",
            pending_dir,
            e,
        )
        return 0

    target = pending_dir / f"{session_id}.json"
    record = {
        "schema_version": 1,
        "session_id": session_id,
        "tool_use_id": payload.get("tool_use_id", "") or "",
        "tool_input": tool_input,
        "written_at": time.time(),
        "input_fingerprint": fingerprint,
        "transcript_path": payload.get("transcript_path", "") or "",
        "cwd": payload.get("cwd", "") or "",
    }

    try:
        atomic_write_json(target, record)
    except OSError as e:
        logger.error("PreToolUse AUQ: write %s failed: %s", target, e)
        return 0

    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    logger.info(
        "PreToolUse AUQ side file: %s tool_use_id=%s fp=%s",
        target.name,
        record["tool_use_id"] or "<none>",
        fingerprint,
    )
    return 0


_EVENT_HANDLERS: dict[str, Callable[[dict], int]] = {
    "SessionStart": _handle_session_start,
    "PreToolUse": _handle_pre_tool_use,
}


def hook_main(argv: list[str] | None = None) -> int:
    """Process a Claude Code hook event from stdin, or install the hook."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="cc-telegram hook",
        description="Claude Code session tracking + AUQ description capture hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hooks (SessionStart + PreToolUse) into ~/.claude/settings.json",
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

    if not isinstance(payload, dict):
        logger.warning("Hook payload is not a dict")
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return 0

    if not isinstance(session_id, str) or not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return 0

    if cwd and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        logger.warning("cwd is not absolute: %s", cwd)
        return 0

    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if entrypoint.startswith("sdk-"):
        source = payload.get("source", "")
        logger.info(
            "Skipping SDK sub-agent hook (entrypoint=%s, source=%s, sid=%s, event=%s)",
            entrypoint,
            source,
            session_id,
            event,
        )
        return 0

    handler = _EVENT_HANDLERS.get(event)
    if handler is None:
        logger.debug("Ignoring unhandled event: %s", event)
        return 0

    try:
        return handler(payload)
    except Exception as e:
        # NEVER propagate. The hook must not block Claude Code's tool
        # execution under any circumstance. Log + exit 0.
        logger.exception("Hook %s handler raised; swallowing: %s", event, e)
        return 0
