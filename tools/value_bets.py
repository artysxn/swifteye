"""Pre-match value bets: buy a FAVOURITE on Polymarket when the bookmakers rate it higher than Polymarket's price, and
hold to the result (one fee, no sale). Found 21 Nov 2026 on 17-20 Nov data; every later day is an unseen test.
Data: ~/swifteye/research/ev_snaps.json (tools/ev_snapshots.py: each settled game at 6 h, 3 h, 1 h, 10 min before the
listed start and 20, 60, 100 min after it: Polymarket bid/ask, every bookmaker group's odds, who won).
  bookmakers' middle = the middle of the bookmaker groups' prices with the margin removed (3+ groups needed)
  the bet's cost      = Polymarket's ask 30 s after the check + the fee 5% x p x (1-p)
  signal              = middle - cost >= 1 point, price 55-90c, spread <= 4c, game not started, quote under 30 min old
One bet a game, at the first check that qualifies.   usage: value_bets.py [from day, e.g. 21]
"""
import json, os, random, sys, statistics as st, datetime as dt
from collections import defaultdict, Counter
R = json.load(open(os.path.expanduser('~/swifteye/research/ev_snaps.json')))
FROM = sys.argv[1] if len(sys.argv) > 1 else None
fee = lambda a: 0.05 * a * (1 - a)
NO = dt.timezone(dt.timedelta(hours=1))


def power(odds):
    pi = [1 / o for o in odds]
    if sum(pi) <= 1.0: return [p / sum(pi) for p in pi]
    lo, hi = 1.0, 6.0
    for _ in range(40):
        k = (lo + hi) / 2
        if sum(p ** k for p in pi) > 1: lo = k
        else: hi = k
    return [p ** k for p in pi]


grp = defaultdict(list)
for r in R: grp[(r['m'], r['t'])].append(r)
for rows in grp.values():
    full = len(rows) == rows[0]['k']
    fams = set.intersection(*[set(r['raw']) for r in rows]) if full else set()
    pw = {}
    for f in fams:
        od = [r['raw'][f] for r in rows]
        if 1.0 <= sum(1 / x for x in od) <= 1.25: pw[f] = dict(zip([r['o'] for r in rows], power(od)))
    for r in rows:
        v = sorted(x[r['o']] for x in pw.values()); r['mid_bk'] = st.median(v) if len(v) >= 3 else None
        r['day'] = dt.datetime.fromtimestamp(r['t'] / 1000, NO).strftime('%d')
random.seed(5)
cost = lambda r: r['ask30'] + fee(r['ask30'])
roi = lambda x: sum(r['won'] - cost(r) for r in x) / sum(cost(r) for r in x) if x else float('nan')


def boot(b, n=1500):
    v = sorted(roi([random.choice(b) for _ in b]) for _ in range(n)); return v[int(n * .05)], v[int(n * .95)]


def first(rows):
    got = {}
    for r in sorted(rows, key=lambda r: r['t']): got.setdefault(r['m'], r)
    return list(got.values())


U = [r for r in R if r['rel'] < 0 and not r['live'] and r.get('ask30') is not None and r['ask'] - r['bid'] <= 0.04 and r['age'] <= 1800 and r['mid_bk'] is not None and (FROM is None or r['day'] >= FROM)]
days = sorted({r['day'] for r in U})
print(f"pre-match rows with 3+ bookmaker groups: {len(U)} in {len({r['m'] for r in U})} games, days {days[0] if days else '-'}-{days[-1] if days else '-'} Nov")


def show(name, x):
    if len(x) < 8: print(f"  {name:44s} {len(x)} bets: too few"); return
    l, h = boot(x)
    byd = ' '.join(f"{d}:{100 * roi(y):+4.0f}%({len(y)})" for d, y in ((d, [r for r in x if r['day'] == d]) for d in days) if y)
    sp = ' '.join(f"{s}:{len(y)}/{100 * roi(y):+.0f}%" for s, y in ((s, [r for r in x if r['sport'] == s]) for s in ('tennis', 'cs2', 'dota2', 'basketball', 'football')) if len(y) >= 6)
    per = sum(r['won'] - cost(r) for r in x) / len(x)
    print(f"  {name:44s} {len(x):4d} bets | {100 * st.mean(r['won'] for r in x):3.0f}% won, needed {100 * st.mean(cost(r) for r in x):3.0f}% | {100 * roi(x):+5.1f}% a $ ({100 * l:+.0f} to {100 * h:+.0f}) | per $10 bet {10 * roi(x):+.2f} | {byd} | {sp}")


print("FAVOURITES 55-90c, by how far the bookmakers' middle is above the bet's cost")
fav = [r for r in U if 0.55 <= r['ask30'] < 0.90]
for a, b, lab in ((-9, -0.02, 'bookmakers 2+ pt BELOW the cost'), (-0.02, 0.0, '0 to 2 pt below'), (0.0, 0.01, '0 to 1 pt above'), (0.01, 0.02, '1 to 2 pt above'), (0.02, 0.03, '2 to 3 pt above'), (0.03, 9, '3+ pt above')):
    show(lab, first([r for r in fav if a <= r['mid_bk'] - cost(r) < b]))
print("THE BET: favourite 55-90c, bookmakers' middle 1+ pt above the cost")
x = first([r for r in fav if r['mid_bk'] - cost(r) >= 0.01]); show('all', x)
for nm, sel in (('6 h before the listed start', -360), ('3 h before', -180), ('1 h before', -60), ('10 min before', -10)):
    show(nm, first([r for r in fav if r['mid_bk'] - cost(r) >= 0.01 and r['rel'] == sel]))
show('without CS2', [r for r in x if r['sport'] != 'cs2'])
show('tennis only', [r for r in x if r['sport'] == 'tennis'])
print("FOR COMPARISON: underdogs 10-45c the bookmakers rate 1+ pt above the cost, and every favourite with no signal")
show('underdogs, signal', first([r for r in U if 0.10 <= r['ask30'] < 0.45 and r['mid_bk'] - cost(r) >= 0.01]))
show('every favourite 55-90c, no signal', first(fav))
