#!/usr/bin/env bash
# Install the main cc-telegram bot as a launchd LaunchAgent (macOS).
#
# Usage:
#   bash bin/install-service.sh           # write the plist + bootstrap + enable
#   bash bin/install-service.sh --print   # dry-run: print the plist it WOULD write, do nothing
#
# Prereq: `cc-telegram` must already be on PATH (install it first with
#   `uv tool install --force --no-cache .`  — see docs/DEPLOYMENT.md section 3).
#
# Idempotent — running it again boots out and re-loads the agent.
# Restart:   launchctl kickstart -k gui/$(id -u)/com.cc-telegram
# Uninstall: launchctl bootout gui/$(id -u)/com.cc-telegram \
#            && rm ~/Library/LaunchAgents/com.cc-telegram.plist

set -euo pipefail

LABEL="com.cc-telegram"
APP_DIR="${CC_TELEGRAM_DIR:-$HOME/.cc-telegram}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Resolve the installed binary (so the plist points at an absolute path —
# launchd's default PATH would not find it otherwise).
BIN="$(command -v cc-telegram || true)"
if [ -z "$BIN" ]; then
    if [ -x "$HOME/.local/bin/cc-telegram" ]; then
        BIN="$HOME/.local/bin/cc-telegram"
    else
        echo "error: cc-telegram not found on PATH." >&2
        echo "       Install it first:  uv tool install --force --no-cache ." >&2
        echo "       (see docs/DEPLOYMENT.md section 3)" >&2
        exit 1
    fi
fi

# launchd starts with a bare PATH; give it ~/.local/bin + homebrew so the bot
# can find `cc-telegram`, `tmux`, and `claude`.
SERVICE_PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

read -r -d '' PLIST_BODY <<EOF || true
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>CC_TELEGRAM_DIR</key>
        <string>$APP_DIR</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>$HOME</string>
    <key>StandardOutPath</key>
    <string>$APP_DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/launchd.err.log</string>
</dict>
</plist>
EOF

if [ "${1:-}" = "--print" ]; then
    echo "$PLIST_BODY"
    exit 0
fi

mkdir -p "$APP_DIR"
mkdir -p "$HOME/Library/LaunchAgents"
printf '%s\n' "$PLIST_BODY" > "$PLIST"

# Replace any existing instance.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $LABEL"
echo "  binary:   $BIN"
echo "  plist:    $PLIST"
echo "  state dir: $APP_DIR"
echo "  logs:     $APP_DIR/launchd.{out,err}.log"
echo ""
echo "Verify:  launchctl print gui/$(id -u)/$LABEL | grep -E 'state|pid'"
echo "Restart: launchctl kickstart -k gui/$(id -u)/$LABEL"
