"""Shared utility functions used across CC Telegram modules.

Provides:
  - app_dir(): resolve config directory from CC_TELEGRAM_DIR env var.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
  - read_cwd_from_jsonl(): extract the cwd field from the first JSONL entry.
  - parse_iso_timestamp(): JSONL ISO8601 timestamp → epoch seconds (None on
    failure) — the SINGLE parse shared by transcript_event_adapter and
    session_monitor so both sides of a timestamp comparison use one clock
    semantics (GH #44).
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

CC_TELEGRAM_DIR_ENV = "CC_TELEGRAM_DIR"


def normalize_background_agent_key(raw: str) -> str:
    """The GH #44 §3.0 single key-normalization contract.

    The async-launch ``agentId`` and the task-notification ``<task-id>`` are
    raw hex ids; the sidechain file stem is ``agent-<id>``. EVERY seam that
    records or queries the route_runtime ``background_agents`` /
    ``background_agents_done`` structures must pass through this helper — a
    join keyed inconsistently would mean launch provenance never attaches to
    activity/done marks (Busy fails to lift, or clears only by TTL). Strips
    ONE leading ``agent-`` prefix; otherwise identity. Lives in utils (the
    shared leaf) because session_monitor deliberately carries no
    route_runtime import; route_runtime re-exports it as public API.
    """
    return raw[6:] if raw.startswith("agent-") else raw


def parse_iso_timestamp(raw: str | None) -> float | None:
    """Parse a JSONL ISO8601 ``timestamp`` to epoch seconds.

    ``None`` on any failure — consumers (the timestamp-qualified notification
    clears, the GH #44 background-agent idle qualification) must FAIL CLOSED
    on an unparseable stamp rather than guess.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def app_dir() -> Path:
    """Resolve config directory from CC_TELEGRAM_DIR or default ~/.cc-telegram."""
    raw = os.environ.get(CC_TELEGRAM_DIR_ENV, "")
    return Path(raw).expanduser() if raw else Path.home() / ".cc-telegram"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it to the
    target path. This prevents data corruption if the process is interrupted
    mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_cwd_from_jsonl(file_path: str | Path) -> str:
    """Read the cwd field from the first JSONL entry that has one."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cwd = data.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""
