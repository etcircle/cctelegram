#!/usr/bin/env bash
# Install a launchd agent that rotates cc-telegram logs every 30 minutes.
#
# Usage: ``bash bin/install-log-rotate.sh``
#
# Idempotent — running it again replaces the existing LaunchAgent.
# Uninstall: ``launchctl bootout gui/$(id -u)/com.cc-telegram.log-rotate
# && rm ~/Library/LaunchAgents/com.cc-telegram.log-rotate.plist``

set -euo pipefail

LOG_DIR="${CC_TELEGRAM_DIR:-$HOME/.cc-telegram}"
SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rotate-logs.sh"
SCRIPT_DEST="$LOG_DIR/rotate-logs.sh"
PLIST="$HOME/Library/LaunchAgents/com.cc-telegram.log-rotate.plist"
LABEL="com.cc-telegram.log-rotate"

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

# Copy (not symlink) the script into ~/.cc-telegram so the LaunchAgent
# doesn't depend on the repo checkout's location.
cp "$SCRIPT_SRC" "$SCRIPT_DEST"
chmod +x "$SCRIPT_DEST"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DEST</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/log-rotate.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CC_TELEGRAM_DIR</key>
        <string>$LOG_DIR</string>
    </dict>
</dict>
</plist>
EOF

# Replace any existing instance.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $LABEL"
echo "  script:   $SCRIPT_DEST"
echo "  plist:    $PLIST"
echo "  interval: every 30 minutes (StartInterval=1800)"
echo "  threshold: 50MB (CC_TELEGRAM_LOG_ROTATE_THRESHOLD_MB to override)"
echo "  retention: 14 days (CC_TELEGRAM_LOG_ROTATE_MAX_AGE_DAYS to override)"
echo ""
echo "Force a run now:"
echo "  launchctl kickstart gui/$(id -u)/$LABEL"
