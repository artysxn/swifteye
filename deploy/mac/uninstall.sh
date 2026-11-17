#!/bin/bash
# Stop the oddsfeed service and remove it from launchd. Keeps ~/swifteye (database, config, logs).
set -euo pipefail
LABEL="com.swifteye.oddsfeed"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Stopped and removed $LABEL. Data is still in ~/swifteye (delete it yourself if you want)."
