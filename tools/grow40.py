"""What $40 becomes over the replayed period under the bot's rules, trade by trade in time order.
Sizing variants: the bot's own (30% of the wallet, at most $12), with and without the half stake in weak games,
and the same without the $12 cap (compounding). A trade's return per dollar is taken from the $12 backtest.
usage: grow40.py"""
import sys, os, math, statistics as st, datetime as dt
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
src = open(os.path.join(HERE, 'systems_backtest.py')).read().split("HDR = f\"")[0]
exec(compile(src, 'systems_backtest_head', 'exec'))
no = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=1))).strftime('%d %b %H:%M')
T = sorted(run(BASE), key=lambda t: t['c']['tf'])
print(f"replayed period: {no(S.ROWS[0]['tf'])} - {no(S.ROWS[-1]['tf'])} Norway time ({(S.ROWS[-1]['tf'] - S.ROWS[0]['tf']) / 86_400_000:.2f} days); the bot's rules take {len(T)} trades")
weak = lambda t: not (F[key(t['c'])]['fl'] is not None and F[key(t['c'])]['fl'] >= 0.2)
def grow(name, pct, cap, half, floor=5.0):
    bal = 40.0; low = bal; peak = bal; dd = 0.0; skipped = 0; day_end = {}; n = 0; worst_day = defaultdict(float)
    for t in T:
        r = t['pnl'] / t['spent']
        stake = min(pct * bal, cap) * (0.5 if half and weak(t) else 1.0)
        stake = min(stake, bal)
        if stake < floor: skipped += 1; continue
        gain = stake * r; bal += gain; n += 1
        worst_day[t['c']['_day']] += gain
        low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal); day_end[t['c']['_day']] = bal
    print(f"  {name:58s} $40 -> ${bal:8.2f} | trades {n} (skipped {skipped}) | lowest ${low:6.2f} | biggest drop ${dd:6.2f} | end of 17 / 18 / 19 / 20 Nov: " + ' / '.join(f"${day_end.get(d, float('nan')):.0f}" for d in ('17', '18', '19', '20')) + f" | worst day {min(worst_day.values()):+.2f}")
print("\n== $40 through the replay")
grow("the sizing until last night: 30% of the wallet, at most $12", 0.30, 12.0, False)
grow("the sizing live now: the same, half stake in weak games", 0.30, 12.0, True)
grow("no $12 cap: 30% of the wallet every trade (compounding)", 0.30, 1e9, False)
grow("no cap, half stake in weak games (compounding)", 0.30, 1e9, True)
grow("no cap, 15% of the wallet, half in weak games", 0.15, 1e9, True)
print("\n== the same without 18 Nov (the day with 90% of the profit)")
T = [t for t in T if t['c']['_day'] != '18']
grow("30% of the wallet, at most $12", 0.30, 12.0, False)
grow("live now: half stake in weak games", 0.30, 12.0, True)
grow("no cap, half stake in weak games (compounding)", 0.30, 1e9, True)
