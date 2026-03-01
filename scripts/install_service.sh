#!/usr/bin/env bash
# Install Atlas as a macOS background service (launchd)
set -e

PLIST_NAME="com.atlas.assistant.plist"
PLIST_SRC="$(dirname "$0")/$PLIST_NAME"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

cp "$PLIST_SRC" "$PLIST_DEST"
launchctl load "$PLIST_DEST"

echo "Atlas service installed and started."
echo "To stop:    launchctl unload $PLIST_DEST"
echo "To restart: launchctl kickstart -k gui/$(id -u)/com.atlas.assistant"
echo "Logs:       tail -f /Users/aleckeith/personal-assistant/logs/atlas.log"
