#!/bin/bash
# Build oddsfeed and run it 24/7 on this Mac as a launchd agent.
# Re-run any time to update (after `git pull`): it rebuilds and restarts the service.
#
#   ./deploy/mac/install.sh
#
# Data lives in ~/swifteye (database, config, logs). Not under ~/Documents:
# macOS privacy rules stop launchd jobs from reading Documents/Desktop/Downloads.
set -euo pipefail

LABEL="com.swifteye.oddsfeed"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOME_DIR="$HOME/swifteye"
BIN="$HOME_DIR/bin/oddsfeed"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust is not installed. Install it first, then re-run this script:"
  echo "  xcode-select --install        # C compiler (needed for SQLite)"
  echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  exit 1
fi

echo "==> Building (release)…"
(cd "$REPO" && cargo build --release)

mkdir -p "$HOME_DIR/bin" "$HOME_DIR/logs"
cp "$REPO/target/release/oddsfeed" "$BIN.new"
mv -f "$BIN.new" "$BIN"

if [ ! -f "$HOME_DIR/oddsfeed.toml" ]; then
  cat > "$HOME_DIR/oddsfeed.toml" <<'EOF'
# See oddsfeed.example.toml in the repo for every option.
bind = "127.0.0.1:8787"   # localhost only: there is no login on the API
db = "oddsfeed.db"
EOF
  echo "==> Wrote $HOME_DIR/oddsfeed.toml"
fi

# caffeinate -i -s keeps the Mac from sleeping while the collector runs
# (-s applies when on power; a closed MacBook lid without an external display still sleeps).
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
    <string>$BIN</string><string>$HOME_DIR/oddsfeed.toml</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>EnvironmentVariables</key>
  <dict><key>RUST_LOG</key><string>info</string></dict>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/oddsfeed.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/oddsfeed.log</string>
</dict>
</plist>
EOF

echo "==> (Re)starting $LABEL…"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# bootout returns before the old job is gone; bootstrap then fails with "5: Input/output error"
# and the collector stays DOWN (seen 2026-11-22). Retry for up to ~20 s.
for i in $(seq 1 10); do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then break; fi
  if [ "$i" = 10 ]; then echo "launchctl bootstrap kept failing; run: launchctl bootstrap gui/$(id -u) $PLIST"; exit 1; fi
  sleep 2
done
launchctl enable "gui/$(id -u)/$LABEL"

for _ in $(seq 1 20); do
  if curl -fs http://127.0.0.1:8787/books >/dev/null; then
    echo "==> Running. Live table: http://127.0.0.1:8787/ui   Logs: $HOME_DIR/logs/oddsfeed.log"
    exit 0
  fi
  sleep 1
done
echo "!! Service did not answer on :8787 yet — check $HOME_DIR/logs/oddsfeed.log"
exit 1
