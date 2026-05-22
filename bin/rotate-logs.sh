#!/usr/bin/env bash
# Rotate cc-telegram launchd logs.
#
# Size-triggered. Runs from launchd every 30 minutes (see
# bin/install-log-rotate.sh). If launchd.err.log or launchd.out.log
# exceeds SIZE_THRESHOLD_MB, gzip a dated copy into the log-archive
# directory then truncate the original in place. Archives older than
# MAX_AGE_DAYS are deleted.
#
# Truncation uses ``: > file`` rather than rename+rotate because the bot
# is launchd-managed and there is no way to signal launchd to reopen its
# stderr FD mid-run. Python's logging writes through stderr with
# O_APPEND, so truncating in place is safe — the next write goes to
# offset 0 (the new end of the empty file), no gap of zeros.
#
# Manual one-off: ``bash bin/rotate-logs.sh`` (idempotent).

set -euo pipefail

LOG_DIR="${CC_TELEGRAM_DIR:-$HOME/.cc-telegram}"
ARCHIVE_DIR="$LOG_DIR/log-archive"
SIZE_THRESHOLD_MB="${CC_TELEGRAM_LOG_ROTATE_THRESHOLD_MB:-50}"
MAX_AGE_DAYS="${CC_TELEGRAM_LOG_ROTATE_MAX_AGE_DAYS:-14}"

mkdir -p "$ARCHIVE_DIR"

for log in launchd.err.log launchd.out.log; do
  src="$LOG_DIR/$log"
  [[ -f "$src" ]] || continue
  size_mb=$(du -m "$src" | cut -f1)
  if (( size_mb >= SIZE_THRESHOLD_MB )); then
    ts=$(date +%Y%m%d-%H%M%S)
    archive="$ARCHIVE_DIR/${log}.${ts}.gz"
    if gzip -c "$src" > "$archive"; then
      : > "$src"
      echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] rotated $log (${size_mb}MB) -> $archive"
    else
      echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] gzip failed for $log; not truncating" >&2
      rm -f "$archive"
    fi
  fi
done

# Prune old archives.
find "$ARCHIVE_DIR" -name '*.gz' -type f -mtime "+$MAX_AGE_DAYS" -delete 2>/dev/null || true
