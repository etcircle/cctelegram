"""Doctor and migration preflight for CC Telegram.

Owns the retry-safe one-shot migration from ~/.ccbot to ~/.cc-telegram, the
bot-start guard that prevents accidental fresh-state startup, and the
fresh-setup health checks emitted by ``cc-telegram doctor``.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from .utils import app_dir

LEGACY_DIR_NAME = ".ccbot"
NEW_DIR_NAME = ".cc-telegram"
OBVIOUS_STATE_FILES = (
    "state.json",
    "session_map.json",
    "monitor_state.json",
    "message_refs.db",
)
REQUIRED_MIGRATION_FILES = (
    "state.json",
    "session_map.json",
    "message_refs.db",
)
MIGRATION_SENTINEL = ".migration-complete"
STAGING_PREFIX = ".cc-telegram.migrating."
LEGACY_SESSION_KEY_PREFIX = "ccbot:"


def _default_legacy_dir() -> Path:
    return Path.home() / LEGACY_DIR_NAME


def migration_command(
    legacy_dir: Path | None = None, new_dir: Path | None = None
) -> str:
    """Return the explicit shell command for copying legacy state."""
    legacy = legacy_dir or _default_legacy_dir()
    target = new_dir or app_dir()
    return f"mkdir -p {shlex.quote(str(target))} && cp -R {shlex.quote(str(legacy))}/. {shlex.quote(str(target))}/"


def migration_needed(
    legacy_dir: Path | None = None, new_dir: Path | None = None
) -> bool:
    """Return True when legacy state exists and the new app dir is absent."""
    legacy = legacy_dir or _default_legacy_dir()
    target = new_dir or app_dir()
    return legacy.exists() and not target.exists()


def _looks_like_legacy_state(target: Path) -> bool:
    if LEGACY_DIR_NAME in target.name:
        return True
    session_map = target / "session_map.json"
    if not session_map.is_file():
        return False
    try:
        data = json.loads(session_map.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(k, str) and k.startswith(LEGACY_SESSION_KEY_PREFIX) for k in data
    )


def preflight_or_exit(
    legacy_dir: Path | None = None,
    new_dir: Path | None = None,
) -> None:
    """Abort bot startup if legacy state needs an explicit migration.

    Setting CC_TELEGRAM_DIR is treated as an explicit operator choice and skips
    the legacy-dir guard. A one-line warning is emitted when the override
    points at legacy-looking state. Hook and doctor subcommands never call
    this function.
    """
    if os.environ.get("CC_TELEGRAM_DIR"):
        target = new_dir or app_dir()
        if _looks_like_legacy_state(target):
            print(
                f"Warning: CC_TELEGRAM_DIR={target} resolves to legacy-looking "
                "state. Run `cc-telegram doctor --migrate` to migrate.",
                file=sys.stderr,
            )
        return
    legacy = legacy_dir or _default_legacy_dir()
    target = new_dir or app_dir()
    if not migration_needed(legacy, target):
        return
    print(
        "State migration required.\n\n"
        "Run:\n"
        "  cc-telegram doctor --migrate\n\n"
        "Manual fallback:\n"
        f"  {migration_command(legacy, target)}\n\n"
        "Or point at a different config dir with CC_TELEGRAM_DIR=/path.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _copy_tree_contents(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _describe_target_state_files(target: Path) -> list[str]:
    """Return human-readable status lines for well-known target state files."""
    return [
        f"  - {name}: {'present' if (target / name).exists() else 'missing'}"
        for name in OBVIOUS_STATE_FILES
    ]


def _cleanup_orphan_staging_dirs(home: Path, target: Path) -> None:
    parent = target.parent
    for child in parent.iterdir():
        if child.name.startswith(STAGING_PREFIX) and child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
    if home != parent:
        for child in home.iterdir():
            if child.name.startswith(STAGING_PREFIX) and child.is_dir():
                shutil.rmtree(child, ignore_errors=True)


def _stage_and_finalize_migration(legacy: Path, target: Path) -> tuple[bool, str]:
    """Stage legacy contents into a sibling dir, validate, atomic-rename.

    Returns (ok, message). On failure the staging dir is cleaned up.
    """
    staging = target.parent / f"{STAGING_PREFIX}{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        _copy_tree_contents(legacy, staging)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"ERROR: failed to stage migration into {staging}: {e}"

    missing = [
        name for name in REQUIRED_MIGRATION_FILES if not (staging / name).exists()
    ]
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        return (
            False,
            "ERROR: staged migration is missing required files: "
            + ", ".join(missing)
            + f"\n  staged from: {legacy}\n  Cleaned up staging dir.",
        )

    sentinel = staging / MIGRATION_SENTINEL
    try:
        sentinel.write_text("ok\n", encoding="utf-8")
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"ERROR: failed to write sentinel into staging dir: {e}"

    try:
        os.rename(staging, target)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"ERROR: failed to finalize migration to {target}: {e}"

    return True, f"Migrated {legacy} -> {target}"


def _check_env_value(name: str, app_dir_path: Path) -> str:
    """Return env var value, falling back to value parsed from app_dir/.env."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_file = app_dir_path / ".env"
    if not env_file.is_file():
        return ""
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() != name:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
                val = val[1:-1]
            return val
    except OSError:
        return ""
    return ""


def _check_hook_installed(settings_file: Path) -> tuple[str, str]:
    """Return (status, detail) where status is OK | WARN | FAIL."""
    if not settings_file.is_file():
        return "FAIL", f"{settings_file} not found"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return "FAIL", f"could not parse {settings_file}: {e}"
    if not isinstance(settings, dict):
        return "FAIL", f"{settings_file} is not a JSON object"
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", []) if isinstance(hooks, dict) else []
    if not isinstance(session_start, list):
        return "FAIL", "hooks.SessionStart is not a list"
    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if isinstance(cmd, str) and "cc-telegram hook" in cmd:
                return "OK", ""
    return "WARN", "SessionStart hook missing"


def _run_health_checks(target: Path) -> tuple[int, int, int]:
    """Print one line per check and return (ok, warn, fail) counts."""
    ok = 0
    warn = 0
    fail = 0

    def emit(status: str, label: str, fix: str = "") -> None:
        nonlocal ok, warn, fail
        if status == "OK":
            ok += 1
            print(f"OK   {label}")
        elif status == "WARN":
            warn += 1
            suffix = f" (fix: {fix})" if fix else ""
            print(f"WARN {label}{suffix}")
        else:
            fail += 1
            suffix = f" (fix: {fix})" if fix else ""
            print(f"FAIL {label}{suffix}")

    token = _check_env_value("TELEGRAM_BOT_TOKEN", target)
    if token:
        emit("OK", "TELEGRAM_BOT_TOKEN")
    else:
        emit(
            "FAIL",
            "TELEGRAM_BOT_TOKEN",
            f"set in {target}/.env or export TELEGRAM_BOT_TOKEN",
        )

    allowed = _check_env_value("ALLOWED_USERS", target)
    if allowed:
        emit("OK", "ALLOWED_USERS")
    else:
        emit(
            "FAIL",
            "ALLOWED_USERS",
            f"set in {target}/.env or export ALLOWED_USERS",
        )

    if shutil.which("tmux"):
        emit("OK", "tmux on PATH")
    else:
        emit("FAIL", "tmux not on PATH", "brew install tmux")

    if shutil.which("claude"):
        emit("OK", "claude on PATH")
    else:
        emit("FAIL", "claude not on PATH", "install Claude Code CLI")

    settings_file = Path.home() / ".claude" / "settings.json"
    hook_status, hook_detail = _check_hook_installed(settings_file)
    label = "SessionStart hook"
    if hook_status == "OK":
        emit("OK", label)
    elif hook_status == "WARN":
        emit("WARN", f"{label}: {hook_detail}", "run `cc-telegram hook --install`")
    else:
        emit("FAIL", f"{label}: {hook_detail}", "run `cc-telegram hook --install`")

    if target.is_dir() and os.access(target, os.W_OK):
        emit("OK", f"config dir writable ({target})")
    else:
        emit(
            "FAIL",
            f"config dir not writable ({target})",
            f"mkdir -p {target} && chmod u+rwx {target}",
        )

    print(f"{ok} ok / {warn} warn / {fail} fail")
    return ok, warn, fail


def doctor_main(argv: list[str] | None = None) -> int:
    """Run migration diagnostics and fresh-setup health checks."""
    parser = argparse.ArgumentParser(
        prog="cc-telegram doctor",
        description="Check CC Telegram config/state migration status and health.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Copy ~/.ccbot contents into ~/.cc-telegram if migration is needed.",
    )
    args = parser.parse_args(argv)

    legacy = _default_legacy_dir()
    target = app_dir()
    home = Path.home()

    sentinel = target / MIGRATION_SENTINEL

    if args.migrate:
        if sentinel.exists():
            print("Migration already complete.")
            return 0
        if not legacy.exists():
            print(f"No legacy state at {legacy}; nothing to migrate.")
            return 0
        if target.exists():
            print("ERROR: migration skipped because target state dir already exists.")
            print(f"  legacy: {legacy}")
            print(f"  target: {target}")
            print("Target obvious state files:")
            for line in _describe_target_state_files(target):
                print(line)
            print("\nNo files were copied to avoid overwriting existing state.")
            print("Review both directories, then migrate manually if intended:")
            print(f"  {migration_command(legacy, target)}")
            return 1
        _cleanup_orphan_staging_dirs(home, target)
        ok, message = _stage_and_finalize_migration(legacy, target)
        print(message)
        return 0 if ok else 1

    if migration_needed(legacy, target):
        print("Migration available:")
        print(f"  legacy: {legacy}")
        print(f"  target: {target}")
        print("Run:")
        print(f"  {migration_command(legacy, target)}")
        print("Or run: cc-telegram doctor --migrate")
        print()
    elif legacy.exists() and target.exists():
        print(f"OK: both legacy and new state dirs exist ({legacy}, {target}).")
        print("Runtime uses only the new state dir unless CC_TELEGRAM_DIR is set.")
        print()
    else:
        print(f"OK: CC Telegram state dir is {target}")
        print()

    _, _, fail = _run_health_checks(target)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(doctor_main())
