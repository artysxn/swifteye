"""A bigger wallet on the clean replay with the bot's rules as they are now (tools/sweep.py S.BASE).
Each trade is re-priced at its own stake against the recorded order book: a bigger buy walks up to 2c past the best
price or is cut to what was really resting, and a bigger sale takes worse prices (sweep.trade).
Stake = 25% of the wallet, half in a weak game (delay score under 0.2), as the live bot sizes real trades.
usage: wallet_sim.py [start, default 400]"""
import sys, os, random, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
START = float(sys.argv[1]) if len(sys.argv) > 1 else 400.0
sys.argv = ['x', 'lib']
import sweep as S
S.MIN_FILL = 5.0
P = dict(S.BASE, legacy=False); ci, gi = 2, 2
T = sorted(S.sim(P), key=lambda t: t['c']['tf'])
fl = lambda c: (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] else None
weak = lambda t: not (fl(t['c']) is not None and fl(t['c']) >= 0.2)
random.seed(40)


def grow(trades, start, pct=0.25, cap=1e12, show=None):
    w = start; low = w; peak = w; dd = 0.0; n = cut = 0; asked = got = 0.0; big = 0.0; ends = {}; worst = 0.0; rows = []
    for t in trades:
        stake = min(pct * w, cap) * (0.5 if weak(t) else 1.0)
        if stake < 5.0: continue
        c = t['c']; r = S.trade(c, c['exits'][ci][gi], dict(P, stake=stake), ci)
        if r is None: continue
        pnl, spent, entry, ex = r
        asked += stake; got += spent; cut += spent < 0.9 * stake; big = max(big, spent); worst = min(worst, pnl)
        w += pnl; n += 1; low = min(low, w); peak = max(peak, w); dd = max(dd, peak - w); ends[c['_day']] = w
        rows.append((c, stake, spent, entry, ex, pnl, w))
    if show:
        print(f"  {show:46s} {start:.0f} -> {w:8.2f} | {n} trades, {cut} cut short by the order book, {100 * got / asked:3.0f}% of the money asked was filled, biggest buy {big:.0f} | worst trade {worst:+.2f}, biggest drop {dd:.2f}, lowest {low:.2f} | end of each day " + ' '.join(f"{d}:{v:.0f}" for d, v in sorted(ends.items())))
    return w, rows


print(f"clean replay 17 Nov 11:49 - 20 Nov 16:56 (3.2 days), today's rules: {len(T)} trades, {sum(t['pnl'] > 0 for t in T)} won at $12 a trade")
w40, _ = grow(T, 40.0, cap=12.0, show='for comparison: 40 wallet, $12 ceiling (now)')
wA, rows = grow(T, START, show=f'{START:.0f} wallet, 25% a trade, no ceiling')
grow(T, START, cap=0.30 * START, show=f'{START:.0f} wallet, ceiling {0.30 * START:.0f} a trade (as $12 is to $40)')
grow(T, START, cap=12.0, show=f"{START:.0f} wallet, today's $12 ceiling left as it is")
print(f"  if the order book took any size, {START:.0f} would have become {START * w40 / 40:.0f} at the same share; the book is why it is less")
print("\n  the biggest trades of the run without a ceiling (asked / really bought / result):")
for c, stake, spent, entry, ex, pnl, w in sorted(rows, key=lambda r: -r[1])[:8]:
    print(f"     {c['_day']} Nov {c['sport']:10s} {(c['home'] + ' v ' + c['away'])[:34]:34s} asked {stake:6.0f}, bought {spent:6.0f} at {100 * entry:4.1f}c, sold at {100 * ex:4.1f}c: {pnl:+7.2f}")
res = sorted(grow([random.choice(T) for _ in T], START)[0] for _ in range(3000))
print(f"\n  the same number of trades drawn again from these {len(T)} (3,000 times), no ceiling: 1 in 10 below {res[300]:.0f}, middle {res[1500]:.0f}, 1 in 10 above {res[2700]:.0f}; under {START:.0f} in {100 * sum(r < START for r in res) / 3000:.0f}% of draws")
# several positions at once
ev = sorted([(t['c']['tf'], 1) for t in T] + [(t['xts'], -1) for t in T]); k = m = 0
for _, d in ev: k += d; m = max(m, k)
print(f"  most positions open at the same moment: {m}")
