"""The strict recording-gap check (tools/hole_check2.py + feed_lag.py) for the moves of the extended replay (SWEEP_DATA=v18)
from 20 Nov on that a loose rule set could buy (7 pt+, followed 20% or less, spread 8c or less, fillable) and that were
never checked. Throttled; saves every 300; waits while the bot's feed is late. Writes hole_gap.json / feed_lag.json,
which tools/sweep.py applies to v18 automatically.
usage: SWEEP_DATA=v18 python3 hole_flag_v18.py [max moves]"""
import sys, os, json, time, subprocess
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
sys.argv = ['x', 'lib']
import hole_check2 as H2, feed_lag as FL
S = H2.T.S


def feed_ok():
    try:
        d = json.loads(subprocess.run(['curl', '-s', '-m', '8', 'http://127.0.0.1:8787/bots'], capture_output=True, text=True).stdout)
        return (d['snipe']['status'].get('pm_lag_ms') or 0) < 600
    except Exception:
        return True


key = H2.T.key
need = [c for c in S.ROWS if c['_day'] >= '20' and not c['nofill'] and c['_mv'] >= 0.07 and c['_fol'] <= 0.20 and c['_sp'] <= 0.08 + 1e-9 and key(c) not in H2.H and key(c) not in FL.L]
if lim: need = need[:lim]
print(f"{len(need)} moves to check", flush=True)
t0 = time.time()
for i, c in enumerate(need):
    FL.lag(c); H2.gap(c)
    if i % 300 == 299:
        json.dump(H2.H, open(H2.CACHE, 'w')); json.dump(FL.L, open(FL.CACHE, 'w'))
        print(f"  {i + 1}/{len(need)}, {time.time() - t0:.0f} s", flush=True)
        while not feed_ok(): print('  bot feed late: waiting 20 s', flush=True); time.sleep(20)
json.dump(H2.H, open(H2.CACHE, 'w')); json.dump(FL.L, open(FL.CACHE, 'w'))
print(f"done: {len(need)} checked in {time.time() - t0:.0f} s", flush=True)
