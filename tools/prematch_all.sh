#!/bin/sh
# Older weeks of Polymarket PRE-MATCH prices + results for several sports, one Polymarket sport tag after another
# (tools/prematch_history.mjs, paced; it waits while the bot's own Polymarket feed runs late).
# Output: ~/swifteye/research/prematch_<name>.ndjson, one line a resolved two-way match market:
#   {q: question, s: listed start (unix s), vol: $ traded, w: index of the winning side (0/1),
#    p: Polymarket's last traded price of side 0 between 6 h and 10 min before the start, age: seconds before the start, n: points}
# Then: python3 tools/fav5060.py ~/swifteye/research/prematch_*.ndjson   (tennis is read from research/hist/ by itself)
# Tags from https://gamma-api.polymarket.com/sports (22 Nov 2026): 864 tennis, 64 esports (cs2 100780, dota2 102366,
# lol 65, val 101672), 745 nba, 28 basketball, 100149 ncaab, 100088 hockey, 899 nhl, 279 ufc, 100350 soccer (3-way: skipped
# by the script), 102883 volleyball, 517 cricket.
# First run 22 Nov 2026 01:02 (bot stopped, real money off). Esports stopped at 5 Nov: fill-in pass for 5-17 Nov below.
# usage: prematch_all.sh [from YYYY-MM-DD] [to YYYY-MM-DD] [name:tag ...]
cd "$(dirname "$0")/.." || exit 1
R=$HOME/swifteye/research
FROM=${1:-2026-10-05}; TO=${2:-2026-11-17}; shift 2 2>/dev/null
PAIRS=${*:-esports:64 nba:745 basketball:28 ncaab:100149 hockey:100088 nhl:899 ufc:279}
for pair in $PAIRS; do
  name=${pair%%:*}; tag=${pair##*:}; out=$R/prematch_$name.ndjson
  [ "$FROM" != 2026-10-05 ] && out=$R/prematch_${name}_$FROM.ndjson
  echo "== $name (tag $tag) $FROM to $TO start $(date +%H:%M)"
  nice -n 19 node tools/prematch_history.mjs $tag $FROM $TO $out 300 4000 2>&1 | tail -n 3
  echo "== $name done $(date +%H:%M): $(wc -l < $out) markets"
done
echo "ALL DONE $(date +%H:%M)"
