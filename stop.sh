#!/bin/bash
# Stop Swifteye. A launchd service (deploy/mac/install.sh) is stopped until the next login or ./start.sh.
LABEL="com.swifteye.oddsfeed"
if [[ "$(uname)" == "Darwin" ]] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kill TERM "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  echo "Service stopped. Run deploy/mac/install.sh (or ./start.sh) to start it again."
fi
pkill -f "target/release/oddsfeed" 2>/dev/null && echo "Collector stopped." || true
