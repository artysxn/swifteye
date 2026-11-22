#!/bin/sh
# DAILY SLACK TEST (owner, 22 Nov 2026: "Test it then"): does a 3-5 s slack in the lagged rule's 'followed' beat exact
# timing on days NONE of these settings were chosen on (22 Nov onward)? Adds the newest replay blocks the app has
# finished, gap-checks the new moves, replays today's rules with 'followed' exact / +3 s / +5 s, and appends the
# result to ~/swifteye/research/slack_test.txt. About 7 minutes; reads the database (best with no real position open).
cd "$(dirname "$0")" || exit 1
export SWEEP_DATA=v18
R=$HOME/swifteye/research
echo "== slack test run $(date '+%d %b %H:%M')" >> $R/slack_test.txt
nice -n 19 python3 sweep_build_v18.py > $R/slack_test_build.log 2>&1 || { echo "  replay build failed (see slack_test_build.log)" >> $R/slack_test.txt; exit 1; }
nice -n 19 python3 hole_flag_v18.py >> $R/slack_test_build.log 2>&1
FROM_MS=1795302000000 SHIFTS=0,3000,5000 nice -n 19 python3 lag_shift.py 2>&1 | grep -v '^!!' | grep -v 'game sides' >> $R/slack_test.txt
tail -n 6 $R/slack_test.txt
