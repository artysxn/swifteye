"""What $40 becomes when the stake is a PERCENTAGE of the wallet (30%: $12 of $40, $120 of $400), trade by trade.
Unlike grow40.py each trade is re-priced at its own stake against the recorded order book: a bigger order walks
past the best price or is cut to what was really resting (tools/sweep.py `trade`).
usage: grow40pct.py"""
import sys, os, datetime as dt
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
src = open(os.path.join(HERE, 'systems_backtest.py')).read().split("HDR = f\"")[0]
exec(compile(src, 'systems_backtest_head', 'exec'))
ci = S.CATCH.index(BASE['catch']); gi = S.GIVE.index(BASE['give'])
S.MIN_FILL = 5.0      # the live bot's smallest real trade since 21 Nov (the backtest's own floor is $10)
T0 = sorted(run(BASE), key=lambda t: t['c']['tf'])
weak = lambda t: not (F[key(t['c'])]['fl'] is not None and F[key(t['c'])]['fl'] >= 0.2)
def grow(name, T, pct, half, cap=1e9, start=40.0):
    bal = start; peak = bal; dd = 0.0; ddp = 0.0; n = 0; cut = 0; day = defaultdict(float); end = {}; big = 0.0; asked = got = 0.0
    for t in T:
        stake = min(pct * bal, cap) * (0.5 if half and weak(t) else 1.0)
        c = t['c']; x = c['exits'][ci][gi]
        r = S.trade(c, x, dict(BASE, stake=stake), ci)
        if r is None: continue
        pnl, spent, entry, ex = r
        asked += stake; got += spent; cut += spent < stake * 0.9
        bal += pnl; n += 1; day[c['_day']] += pnl; end[c['_day']] = bal; big = max(big, spent)
        peak = max(peak, bal); dd = max(dd, peak - bal); ddp = max(ddp, (peak - bal) / peak)
    print(f"  {name:52s} ${start:.0f} -> ${bal:8.2f} | {n} trades, {cut} cut short by the book, {got / asked * 100:3.0f}% of the money asked was filled, biggest buy ${big:.0f} | biggest drop ${dd:.0f} ({ddp * 100:.0f}% of the wallet) | end of day: " + ' / '.join(f"${end.get(d, float('nan')):.0f}" for d in ('17', '18', '19', '20')) + f" | worst day {min(day.values()):+.0f}")
print(f"replay 17 Nov 11:49 - 20 Nov 16:56, {len(T0)} trades; stake as a share of the wallet at each trade")
grow('30% of the wallet (your sizing)', T0, 0.30, False)
grow('30%, half stake in weak games (live rule)', T0, 0.30, True)
grow('25%, half stake in weak games', T0, 0.25, True)
grow('25% of the wallet, no half-stake rule', T0, 0.25, False)
grow('20%, half in weak games', T0, 0.20, True)
grow('15%, half in weak games', T0, 0.15, True)
grow('for comparison: 30% but never over $12 (as set now)', T0, 0.30, True, cap=12.0)
print('without 18 Nov:')
T1 = [t for t in T0 if t['c']['_day'] != '18']
grow('30% of the wallet', T1, 0.30, False)
grow('30%, half stake in weak games', T1, 0.30, True)
grow('25%, half stake in weak games', T1, 0.25, True)
print('a bigger wallet, 30% with half in weak games (does the book hold the size?):')
for s in (400.0, 4000.0): grow(f"start ${s:.0f}", T0, 0.30, True, start=s)
