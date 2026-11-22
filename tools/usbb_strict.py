"""US basketball (NBA / college) - the bulk of the off-prime moves - under STRICTER rules? (tools/offprime.py: allowing it as is
costs -$20). Only US-basketball trades are scored; the rest of the bot unchanged. Replay as offprime.py (5 s slack, 15-min give-up)."""
import os, sys, io, contextlib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
S = LS.S; ROWS = LS.rows_with(5000)
US = [c for c in ROWS if c['sport'] == 'basketball' and c['_usbb']]
B = dict(S.BASE, real_sale=True, give=900, bb='all')
days = sorted({c['_day'] for c in ROWS})
out = ['US BASKETBALL ONLY (%d candidate moves, %d games), stricter rules; $12 flat, 5 s slack, 15-min give-up' % (len(US), len({c['match_id'] for c in US}))]
res = []
for name, kw in (('as the other sports', {}), ('lagged 60%', dict(lag=0.6)), ('lagged 70%', dict(lag=0.7)), ('3 families', dict(fams=3)), ('consensus 5 pt', dict(cons=0.05)),
                 ('spread 2c', dict(max_spread=0.02)), ('spread 1c', dict(max_spread=0.01)), ('gain 6c', dict(min_edge=0.06)), ('move 10 pt', dict(min_move=0.10)), ('move 12 pt', dict(min_move=0.12)),
                 ('followed 0%', dict(max_followed=0.0)), ('lagged 60% + 3 families', dict(lag=0.6, fams=3)), ('lagged 60% + spread 2c', dict(lag=0.6, max_spread=0.02)),
                 ('move 10 pt + 3 families', dict(min_move=0.10, fams=3))):
    T = S.sim(dict(B, **kw), rows=US); by = collections.defaultdict(float)
    for t in T: by[t['c']['_day']] += t['pnl']
    res.append((name, [by[d] for d in days], len(T)))
    out.append('  %-26s %3d trades, won %3d, %+7.2f $ | by day %s' % (name, len(T), sum(1 for t in T if t['pnl'] > 0), sum(t['pnl'] for t in T), ' / '.join('%+.1f' % by[d] for d in days)))
tot = 0.0
for i, d in enumerate(days):
    pick = max(res, key=lambda r: sum(r[1]) - r[1][i]); tot += pick[1][i]
out.append('  leave one day out over these: %+.2f $ (skipping US basketball: 0 $)' % tot)
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/usbb_strict.txt'), 'w').write(txt + '\n')
