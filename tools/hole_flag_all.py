"""Measure the recording-gap flag for EVERY tradable move of the backtest set (tools/sweep.py), so the set can be
replayed without the buys that sit inside gaps of the Polymarket recording. Throttled; waits while the bot's feed is late.
Writes ~/swifteye/research/hole_gap.json, feed_lag.json and, when every move is done, hole_gap_full.flag.
usage: hole_flag_all.py"""
import sys, os, json, time, subprocess
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import hole_check2 as H2, feed_lag as FL
T = H2.T; S = T.S


def feed_ok():
    try:
        d = json.loads(subprocess.run(['curl', '-s', '-m', '8', 'http://127.0.0.1:8787/bots'], capture_output=True, text=True).stdout)
        return (d['snipe']['status'].get('pm_lag_ms') or 0) < 600
    except Exception:
        return True


need = [c for c in S.ROWS if not c['nofill'] and T.key(c) not in H2.H]
print(f"{len(need)} moves to measure", flush=True)
t0 = time.time()
for i, c in enumerate(need):
    FL.lag(c); H2.gap(c)
    if i % 500 == 499:
        json.dump(H2.H, open(H2.CACHE, 'w')); json.dump(FL.L, open(FL.CACHE, 'w'))
        print(f"  {i + 1}/{len(need)}, {time.time() - t0:.0f} s", flush=True)
        while not feed_ok(): print('  bot feed late: waiting 20 s', flush=True); time.sleep(20)
json.dump(H2.H, open(H2.CACHE, 'w')); json.dump(FL.L, open(FL.CACHE, 'w'))
open(os.path.expanduser('~/swifteye/research/hole_gap_full.flag'), 'w').write(time.strftime('%Y-%m-%d %H:%M:%S') + f" {len(H2.H)} moves\n")
print(f"done: {len(H2.H)} moves flagged, {time.time() - t0:.0f} s", flush=True)
