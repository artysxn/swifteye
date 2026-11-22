"""Are the quiet stretches on Polymarket after a bookmaker move REAL, or holes in the recording?
For sampled candidate moves: the stretch between Polymarket's last quote before the move and its first one after
(both by Polymarket's own clock). During that stretch, did OTHER games that were active around then get any quote rows?
If the other games are silent too, the recorder was not receiving: a hole. usage: hole_check.py"""
import sys, os, json, random, sqlite3, bisect, statistics as st
from collections import defaultdict
sys.argv = ['x', 'lib']
import sweep as S
G = json.load(open(os.path.expanduser('~/swifteye/research/tells_feats.json')))
key = lambda c: f"{c['match_id']}|{c['outcome']}|{c['t0']}"
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
random.seed(9)
by = defaultdict(list)
for c in S.ROWS:
    if key(c) in G: by[c['_day']].append(c)
allrows = sorted(S.ROWS, key=lambda c: c['t0']); T0 = [c['t0'] for c in allrows]
print("day | moves sampled | quiet stretch around the move, s (median) | of the other active games checked: share with NO quote in that stretch | their usual quotes per 10 s | verdict")
out = {}
for d in sorted(by):
    res = []
    for c in random.sample(by[d], 60):
        q = [r[0] for r in db.execute("SELECT COALESCE(server_ts_ms, ts_ms) FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND outcome=? ORDER BY 1", (c['match_id'], c['t0'] - 150_000, c['t0'] + 200_000, c['outcome']))]
        i = bisect.bisect_right(q, c['t0'])
        if i == 0 or i >= len(q): continue
        a, b = q[i - 1], q[i]
        # other games with a recorded bookmaker move within 10 min (so they were live)
        lo, hi = bisect.bisect_left(T0, c['t0'] - 600_000), bisect.bisect_right(T0, c['t0'] + 600_000)
        others = list({x['match_id'] for x in allrows[lo:hi] if x['match_id'] != c['match_id']})[:8]
        silent = act = 0; rate = []
        for m in others:
            o = [r[0] for r in db.execute("SELECT COALESCE(server_ts_ms, ts_ms) FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? ORDER BY 1", (m, a - 120_000, b + 120_000))]
            base = sum(1 for t in o if a - 60_000 <= t < a)
            if base < 6: continue            # that game was not being quoted then
            act += 1; rate.append(base / 6)
            if not any(a < t < b for t in o): silent += 1
        res.append(dict(gap=(b - a) / 1000, after=(b - c['t0']) / 1000, act=act, silent=silent, rate=st.mean(rate) if rate else None, q_upd=G[key(c)].get('q_upd')))
    out[d] = res
    for nm, sel in (('all', res), ('no re-quote before the buy', [r for r in res if r['q_upd'] == 0]), ('re-quoted', [r for r in res if r['q_upd']])):
        x = [r for r in sel if r['act'] >= 2]
        if not x: continue
        sh = sum(r['silent'] for r in x) / sum(r['act'] for r in x)
        exp = st.mean(pow(2.718, -r['rate'] * r['gap'] / 10) for r in x)   # chance a normally-quoted game shows nothing in a stretch that long
        print(f"  {d} {nm:28s} | {len(x):3d} | {st.median(r['gap'] for r in x):6.1f} | {100 * sh:3.0f}% (if quotes kept flowing: about {100 * exp:.0f}%) | {st.mean(r['rate'] for r in x):4.1f} | all {sum(r['silent'] == r['act'] for r in x)} of {len(x)} moves had EVERY other game silent")
json.dump(out, open(os.path.expanduser('~/swifteye/research/hole_check.json'), 'w'))
