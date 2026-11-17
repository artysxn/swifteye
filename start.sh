#!/bin/bash
# Start Swifteye (collector + web app) and open it in the browser.
#   ./start.sh
# If the 24/7 service from deploy/mac/install.sh is installed, this just (re)starts it.
# Otherwise it builds and runs the collector in the background with data in ~/swifteye.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8787"
LABEL="com.swifteye.oddsfeed"
DATA="$HOME/swifteye"

open_app() { command -v open >/dev/null && open "$URL" || xdg-open "$URL" >/dev/null 2>&1 || echo "Open $URL"; }

if curl -fs "$URL/books" >/dev/null 2>&1; then
  echo "Swifteye is already running."
  open_app
  exit 0
fi

if [[ "$(uname)" == "Darwin" ]] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "Starting the installed service…"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
else
  if ! command -v cargo >/dev/null 2>&1; then
    echo "Rust is not installed:"
    echo "  xcode-select --install"
    echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    exit 1
  fi
  echo "Building…"
  (cd "$REPO" && cargo build --release)
  mkdir -p "$DATA/logs"
  [ -f "$DATA/oddsfeed.toml" ] || printf 'bind = "127.0.0.1:8787"\ndb = "oddsfeed.db"\n' > "$DATA/oddsfeed.toml"
  echo "Starting the collector (logs: $DATA/logs/oddsfeed.log)…"
  (cd "$DATA" && nohup "$REPO/target/release/oddsfeed" oddsfeed.toml >> logs/oddsfeed.log 2>&1 &)
fi

for _ in $(seq 1 30); do
  if curl -fs "$URL/books" >/dev/null 2>&1; then
    echo "Running at $URL"
    open_app
    exit 0
  fi
  sleep 1
done
echo "It did not come up in 30 s. Check $DATA/logs/oddsfeed.log"
exit 1
