"""One rule change at a time, judged honestly (leave one day out), on the extended replay with 'followed' exact / +3 s / +5 s.
For each held-out day: the single change (or today's rules) with the most $ on the other 4 days, scored on that day.
usage: SWEEP_DATA=v18 python3 rule_single_loo.py      -> ~/swifteye/research/rule_single_loo.txt"""
import os, sys, io, contextlib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '0,3000,5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
S = LS.S
V = {'exact': LS.rows_with(0), '+3 s': LS.rows_with(3000), '+5 s': LS.rows_with(5000)}
days = sorted({c['_day'] for c in S.ROWS})
SPACE = dict(min_move=[0.07, 0.09, 0.10], max_followed=[0.0, 0.10, 0.15], fams=[1, 3], cons=[0.0, 0.02, 0.05], max_spread=[0.02, 0.03, 0.06],
             min_edge=[0.0, 0.02, 0.06], lag=[None, 0.3, 0.4, 0.6], weak_cap=[None, 0.7, 0.8], gap=[None, 0.05], cap=[0.85, 0.95], bb=['none', 'all'],
             pause=[False], catch=[0.5, 0.75], give=[120, 900, 1800], dota_spread=[None])
B = dict(S.BASE, real_sale=True)
def score(kw, rows):
    T = S.sim(dict(B, **kw), rows=rows); by = collections.defaultdict(float)
    for t in T: by[t['c']['_day']] += t['pnl']
    return [by[d] for d in days], len(T)
res = []
for v, rows in V.items():
    res.append((v, 'today\'s rules', *score({}, rows)))
    for k, vals in SPACE.items():
        for x in vals: res.append((v, '%s=%s' % (k, x), *score({k: x}, rows)))
out = ['ONE CHANGE AT A TIME (%d candidates incl. the slack), extended replay %s Nov, real-money sales, $12 flat' % (len(res), '/'.join(days))]
out.append('\nIN-SAMPLE, best 15 single changes by total $ (today\'s rules exact: %+.2f $):' % sum([r for r in res if r[0] == 'exact' and r[1] == "today's rules"][0][2]))
for v, name, pd, n in sorted(res, key=lambda r: -sum(r[2]))[:15]:
    out.append('  %+8.2f $ %3d trades | %s | slack %-5s | %s' % (sum(pd), n, ' / '.join('%+.1f' % x for x in pd), v, name))
out.append('\nLEAVE ONE DAY OUT (pick the best single change on 4 days, score it on the 5th):')
tot = 0.0; base = [r for r in res if r[0] == 'exact' and r[1] == "today's rules"][0][2]
for i, d in enumerate(days):
    pick = max(res, key=lambda r: sum(r[2]) - r[2][i]); tot += pick[2][i]
    out.append('  %s Nov: picked %s %s -> %+.2f $ on %s Nov (today\'s rules %+.2f $)' % (d, pick[0], pick[1], pick[2][i], d, base[i]))
out.append('  SUM: single-change search %+.2f $ v today\'s rules %+.2f $' % (tot, sum(base)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/rule_single_loo.txt'), 'w').write(txt + '\n')
