"""The pre-match favourite bet on tennis matches from BEFORE Swifteye's recording (tools/hist_tennis.py).
Per match: the bookmakers' AVERAGE closing odds (betexplorer), Polymarket's price shortly before the start (its own
price history), and Polymarket's own settlement.
  bookmakers   average odds of both players, margin removed by the power method (as the bot does)
  the price    Polymarket's last price at least 5 minutes before the match's start; the bet costs that + 1c (half a
               spread, the history has no ask) + the fee
  the bet      the favourite's cost price 55-90c and the bookmakers 1+ point above the cost; held to the result
Not the same as the live bet in two ways: one average instead of the middle of 3+ platforms, and CLOSING odds (the
bookmakers' last word, a little later than the Polymarket price used). The second favours the bet, so the same test
is also run with Polymarket's price 60 minutes earlier, where that advantage is larger: if the result grew a lot there,
the edge would be the bookmakers' later information, not a mispriced favourite.
usage: hist_test.py [joined.json]"""
import sys, os, json, glob, random, bisect, statistics as st, datetime as dt
from collections import defaultdict, Counter
H = os.path.expanduser('~/swifteye/research/hist/')
F = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(H + 'joined_*.json'))[-1]
J = json.load(open(F))
fee = lambda a: 0.05 * a * (1 - a)
random.seed(9)


def power(odds):
    pi = [1 / o for o in odds]
    if sum(pi) <= 1.0: return [p / sum(pi) for p in pi]
    lo, hi = 1.0, 6.0
    for _ in range(40):
        k = (lo + hi) / 2
        if sum(p ** k for p in pi) > 1: lo = k
        else: hi = k
    return [p ** k for p in pi]


def price_at(hist, t):
    i = bisect.bisect_right([x[0] for x in hist], t)
    return (hist[i - 1][1], t - hist[i - 1][0]) if i else None


def rows(before=300, max_age=6 * 3600, half_spread=0.01):
    out = []
    for m in J:
        if len(m['res']) != 2 or not m['hist']: continue
        over = 1 / m['odds'][0] + 1 / m['odds'][1]
        if not (1.0 <= over <= 1.15): continue
        q = price_at(m['hist'], m['start'] - before)
        if not q or q[1] > max_age or not (0.02 < q[0] < 0.98): continue
        fair = power(m['odds'])
        for s in (0, 1):
            p = q[0] if s == 0 else 1 - q[0]
            c = p + half_spread; cost = c + fee(c)
            out.append(dict(m=m, s=s, price=c, cost=cost, fair=fair[s], edge=fair[s] - cost, won=m['res'][s], void=abs(m['res'][s] - 0.5) < 0.01, age=q[1],
                            day=dt.datetime.fromtimestamp(m['start'], dt.UTC).strftime('%m-%d'), week=dt.datetime.fromtimestamp(m['start'], dt.UTC).strftime('%V'),
                            tier=m['tour'].split('__')[1] if '__' in m['tour'] else m['tour'], vol=m['vol']))
    return out


roi = lambda x: sum(r['won'] - r['cost'] for r in x) / sum(r['cost'] for r in x) if x else float('nan')


def boot(b, n=2000):
    v = sorted(roi([random.choice(b) for _ in b]) for _ in range(n)); return v[int(n * .05)], v[int(n * .95)]


def show(name, x):
    if len(x) < 10: print(f"  {name:56s} {len(x):5d} bets: too few"); return
    l, h = boot(x)
    print(f"  {name:56s} {len(x):5d} bets | {100 * st.mean(r['won'] for r in x):4.1f}% won, needed {100 * st.mean(r['cost'] for r in x):4.1f}% | {100 * roi(x):+5.1f}% a $ ({100 * l:+.1f} to {100 * h:+.1f})")


R = [r for r in rows() if not r['void']]
days = sorted({r['day'] for r in R})
print(f"{len(J)} matches joined; {len({id(r['m']) for r in R})} with a Polymarket price before the start and a result, {days[0]} to {days[-1]}; tiers {Counter(r['tier'] for r in R if r['s'] == 0).most_common(6)}")
fav = [r for r in R if 0.55 <= r['price'] < 0.90]
print("\nFAVOURITES 55-90c by how far the bookmakers' average is above the bet's cost")
for a, b, lab in ((-9, -0.02, 'bookmakers 2+ pt BELOW the cost'), (-0.02, 0.0, '0 to 2 pt below'), (0.0, 0.01, '0 to 1 pt above'), (0.01, 0.02, '1 to 2 pt above'), (0.02, 0.03, '2 to 3 pt above'), (0.03, 9, '3+ pt above')):
    show(lab, [r for r in fav if a <= r['edge'] < b])
bet = [r for r in fav if r['edge'] >= 0.01]
print("\nTHE BET (1+ pt above), and what it should beat")
show('THE BET', bet)
show('every favourite 55-90c, no signal', fav)
show('favourites the bookmakers rate below the cost', [r for r in fav if r['edge'] < 0])
k = len(bet); obs = roi(bet); N = 3000
cnt = sum(1 for _ in range(N) if roi(random.sample(fav, k)) >= obs) if k and len(fav) > k else 0
print(f"  {k} favourites picked at random do as well as the bet in {100 * cnt / N:.1f}% of {N} draws")
print("\nBY WEEK, TOUR LEVEL, PRICE, MONEY TRADED")
for w in sorted({r['week'] for r in bet}): show(f'week {w}', [r for r in bet if r['week'] == w])
for t, n in Counter(r['tier'] for r in bet).most_common(8): show(t, [r for r in bet if r['tier'] == t])
for lo, hi in ((0.55, 0.65), (0.65, 0.75), (0.75, 0.90)): show(f'price {100 * lo:.0f}-{100 * hi:.0f}c', [r for r in bet if lo <= r['price'] < hi])
for lo, hi in ((0, 2000), (2000, 20000), (20000, 10 ** 12)): show(f'traded ${lo:,} to ${hi:,}' if hi < 10 ** 11 else f'traded ${lo:,}+', [r for r in bet if lo <= r['vol'] < hi])
print("\nCHECKS")
show('threshold 2 pt', [r for r in fav if r['edge'] >= 0.02]); show('threshold 0.5 pt', [r for r in fav if r['edge'] >= 0.005])
show('price history under 30 min old at the bet', [r for r in bet if r['age'] <= 1800])
x = [r for r in rows(half_spread=0.02) if not r['void'] and 0.55 <= r['price'] < 0.90 and r['edge'] >= 0.01]; show('if the spread costs 2c instead of 1c', x)
x = [r for r in rows(before=3600) if not r['void'] and 0.55 <= r['price'] < 0.90 and r['edge'] >= 0.01]; show('Polymarket price 60 min before the start', x)
x = [r for r in rows(before=3 * 3600) if not r['void'] and 0.55 <= r['price'] < 0.90 and r['edge'] >= 0.01]; show('Polymarket price 3 h before the start', x)
x = [r for r in rows(before=60) if not r['void'] and 0.55 <= r['price'] < 0.90 and r['edge'] >= 0.01]; show('Polymarket price 1 min before the start', x)
und = [r for r in R if 0.10 <= r['price'] < 0.45 and r['edge'] >= 0.01]; show('underdogs 10-45c with the same signal', und)
v = [r for r in rows() if r['void']]; print(f"  matches Polymarket settled 50-50 (called off): {len(v) // 2}, left out")
print("\nSTAKE: $40, 4% of the wallet a bet, bets in order of start (settled before the next day's bets for simplicity)")
w = 40.0; low = w; byw = {}
for r in sorted(bet, key=lambda r: r['m']['start']):
    s = 0.04 * w; w += s / r['cost'] * r['won'] - s; low = min(low, w); byw[r['week']] = w
span = (max(r['m']['start'] for r in bet) - min(r['m']['start'] for r in bet)) / 86400 if bet else 0
print(f"  {len(bet)} bets over {span:.0f} days ({len(bet) / max(span, 1):.1f} a day): $40 -> ${w:.2f}, lowest ${low:.2f}; end of each week " + ' '.join(f"w{k}:{v:.0f}" for k, v in sorted(byw.items())))
