"""The bot after the 22 Nov change (5 s slack in 'followed', give up at 15 min) v before, on the extended replay 17 Nov 05:00 -
22 Nov 01:00 (SWEEP_DATA=v18, strict gap checks, real-money sales). 'Old, feed on time' = 'followed' on Polymarket's own clock
with no slack and a 5-min give-up (how the old bot behaves now that the feed is on time); 'old, as it ran' = arrival time
(the late feed of 17-18 Nov). Flat $12 a trade, and the $40 wallet (tools/wallet_best.py sizing)."""
import os, sys, io, contextlib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '0,3000,5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
    import wallet_best as WB
S = LS.S
B = dict(S.BASE, real_sale=True)
out = ['NEW BOT v OLD BOT, replay 17 Nov 05:00 - 22 Nov 01:00, real-money sales']
out.append('  %-44s %s' % ('', 'flat $12 a trade                                   | $40 wallet (real-money sizing)'))
for lab, rows, kw in (('NEW: 5 s slack + give up at 15 min', LS.rows_with(5000), dict(give=900)), ('OLD, feed on time (how it runs now)', LS.rows_with(0), {}), ('OLD, as it ran (late feed 17-18 Nov)', S.ROWS, {})):
    T = S.sim(dict(B, **kw), rows=rows); w = [t['pnl'] for t in T if t['pnl'] > 0]; l = [t['pnl'] for t in T if t['pnl'] <= 0]
    by = collections.defaultdict(lambda: [0, 0])
    for t in T: by[t['c']['_day']][0] += 1; by[t['c']['_day']][1] += t['pnl'] > 0
    bal, n, ww, dd, worst, _ = WB.wallet(rows, kw)
    out.append('  %-44s %3d trades, won %3d = %3.0f%%, win %+.2f loss %+.2f, %+7.2f $ | $40 -> $%.2f, %d trades, won %d = %.0f%%' % (
        lab, len(T), len(w), 100.0 * len(w) / max(1, len(T)), sum(w) / max(1, len(w)), sum(l) / max(1, len(l)), sum(w) + sum(l), bal, n, ww, 100.0 * ww / max(1, n)))
    out.append('      win rate by day: ' + ' | '.join('%s Nov %d/%d' % (d, v[1], v[0]) for d, v in sorted(by.items())))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/new_vs_old.txt'), 'w').write(txt + '\n')
