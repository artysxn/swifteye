"""The widened rule sets (tools/rule_adjust.py, chosen on 17-20 Nov) on days they were NOT chosen on: 21 Nov and the first
hours of 22 Nov, from the extended replay (tools/sweep_build_v18.py; run with SWEEP_DATA=v18). Same replay fills and
exits (target 100%, give up at 5 min), $12, the bot's locks, clean (no buys in recording gaps).
usage: SWEEP_DATA=v18 python3 rule_oos.py      -> ~/swifteye/research/rule_oos.txt
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

B = S.BASE
days = sorted({c['_day'] for c in S.ROWS})
SEEN = {'17', '18', '19', '20'}
SETS = [("TODAY'S RULES", {}),
        ('expected gain 0c + lagged 40%', dict(min_edge=0.0, lag=0.4)),
        ('expected gain 0c + weak cap 70c + lagged 40%', dict(min_edge=0.0, weak_cap=0.70, lag=0.4)),
        ('followed 10% + lagged 40%', dict(max_followed=0.10, lag=0.4)),
        ('expected gain 0c + followed 10% + lagged 40%', dict(min_edge=0.0, max_followed=0.10, lag=0.4)),
        ('expected gain 0c + followed 10% + weak cap 70c + lagged 40%', dict(min_edge=0.0, max_followed=0.10, weak_cap=0.70, lag=0.4)),
        ('expected gain 0c', dict(min_edge=0.0)), ('expected gain 2c', dict(min_edge=0.02)), ('lagged 40%', dict(lag=0.4)), ('followed 10%', dict(max_followed=0.10)),
        ('spread 6c', dict(max_spread=0.06)), ('weak cap 70c', dict(weak_cap=0.70)), ('moves 7 pt', dict(min_move=0.07))]
out = ['WIDENED RULES ON UNSEEN DAYS: replay data %s, days %s Nov (chosen on 17-20; unseen: %s)\n' % (os.environ.get('SWEEP_DATA', 'v16'), '/'.join(days), '/'.join(d for d in days if d not in SEEN))]
for name, kw in SETS:
    T = S.sim(dict(B, **kw)); by = collections.defaultdict(list)
    for t in T: by[t['c']['_day']].append(t['pnl'])
    seen = [p for d in days if d in SEEN for p in by[d]]; uns = [p for d in days if d not in SEEN for p in by[d]]
    f = lambda X: '%3d trades, won %3.0f%%, %+7.2f $' % (len(X), 100.0 * sum(1 for p in X if p > 0) / max(1, len(X)), sum(X))
    out.append('  %-60s 17-20 Nov: %s || UNSEEN: %s || by day %s' % (name, f(seen), f(uns), ' / '.join('%s:%+.1f(%d)' % (d, sum(by[d]), len(by[d])) for d in days)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/rule_oos.txt'), 'w').write(txt + '\n')
