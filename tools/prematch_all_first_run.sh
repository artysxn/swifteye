#!/bin/sh
# Older weeks of Polymarket pre-match prices + results, one sport tag after another (paced by the script itself).
cd /Users/danielvinichenko/swifteye-src
R=/Users/danielvinichenko/swifteye/research
for pair in esports:64 nba:745 basketball:28 ncaab:100149 hockey:100088 nhl:899 ufc:279; do
  name=${pair%%:*}; tag=${pair##*:}
  echo "== $name (tag $tag) start $(date +%H:%M)"
  nice -n 19 node tools/prematch_history.mjs $tag 2026-10-05 2026-11-17 $R/prematch_$name.ndjson 300 4000 2>&1 | tail -n 3
  echo "== $name done $(date +%H:%M): $(wc -l < $R/prematch_$name.ndjson) markets"
done
echo "ALL DONE $(date +%H:%M)"
