"""The open oddity of 17-21 Nov (sides costing 50-60c won 65-67%), checked on matches from BEFORE Swifteye's recording.

Patterns first: what did the side costing X cents win, band by band, on the older weeks? No bet rule chosen up front.
  tennis   tools/hist_tennis.py's joined file (Polymarket price history + Polymarket's settlement, 5 Oct - 16 Nov)
  others   tools/prematch_history.mjs files (.ndjson: price 10 min to 6 h before the listed start, settlement)
Price = Polymarket's last traded price at the moment; the bet costs that + 1c (half a spread) + the fee 0.05 p (1 - p).
One row a market: the FAVOURITE (the side above 50c), and separately the underdog.

usage: fav5060.py [file.ndjson ...]      (no files: tennis only)
Saved results: ~/swifteye/research/fav5060_results.txt (python3 tools/fav5060.py ~/swifteye/research/prematch_*.ndjson > that file)
"""
import sys, os, re, json, glob, bisect, random, statistics as st, datetime as dt
from collections import defaultdict
fee = lambda a: 0.05 * a * (1 - a)
random.seed(11)
H = os.path.expanduser('~/swifteye/research/hist/')


def tennis(before=600):
    J = json.load(open(sorted(glob.glob(H + 'joined_*.json'))[-1])); out = []
    for m in J:
        if len(m['res']) != 2 or not m['hist'] or abs(m['res'][0] - 0.5) < 0.01: continue
        ts = [x[0] for x in m['hist']]; i = bisect.bisect_right(ts, m['start'] - before)
        if not i: continue
        t, p = m['hist'][i - 1]
        if m['start'] - t > 6 * 3600 or not (0.02 < p < 0.98) or abs(p - 0.5) < 1e-9: continue
        out.append(dict(sport='tennis', s=m['start'], p=p, w=0 if m['res'][0] > 0.5 else 1, vol=m['vol'], age=m['start'] - t))
    return out


def ndjson(f, sport):
    out = []
    for l in open(f):
        if not l.strip(): continue
        r = json.loads(l)
        if not (0.02 < r['p'] < 0.98) or abs(r['p'] - 0.5) < 1e-9: continue
        out.append(dict(sport=sport, s=r['s'], p=r['p'], w=r['w'], vol=r['vol'], age=r['age'], q=r['q']))
    return out


def side(r, fav=True):
    p = r['p']; zero = (p > 0.5) == fav
    return (p if zero else 1 - p), int((r['w'] == 0) == zero)


def rng(x, n=1000):
    v = sorted(st.mean(random.choice(x) for _ in x) for _ in range(n)); return v[int(n * .05)], v[int(n * .95)]


def show(name, X, fav=True):
    B = [side(r, fav) for r in X]
    if len(B) < 30: print(f"  {name:30s} {len(B):5d} (too few)"); return
    cost = lambda p: p + 0.01 + fee(p + 0.01)
    ret = [(w - cost(p)) / cost(p) for p, w in B]; lo, hi = rng(ret)
    won = st.mean(w for _, w in B); said = st.mean(p for p, _ in B)
    print(f"  {name:30s} {len(B):5d} | price {said * 100:5.1f}c | won {won * 100:5.1f}% (+/-{(won * (1 - won) / len(B)) ** 0.5 * 100:.1f}) | off by {(won - said) * 100:+5.1f} pt | as a bet: {st.mean(ret) * 100:+6.1f}% a $ ({lo * 100:+.0f} to {hi * 100:+.0f})")


if __name__ == '__main__':
    R = tennis()
    seen = set()
    for f in sys.argv[1:]:
        for r in ndjson(f, re.sub(r'_\d{4}-\d\d-\d\d$', '', os.path.basename(f).split('.')[0].replace('prematch_', ''))):   # fill-in files: prematch_<sport>_<from>.ndjson
            if (r['q'], r['s']) in seen: continue          # the same market under two tags (nba + basketball, nhl + hockey)
            seen.add((r['q'], r['s'])); R.append(r)
    bands = ((0.50, 0.55), (0.55, 0.60), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90))
    for sport in sorted({r['sport'] for r in R}) + ['ALL']:
        X = R if sport == 'ALL' else [r for r in R if r['sport'] == sport]
        d0, d1 = (dt.datetime.fromtimestamp(f(r['s'] for r in X), dt.UTC).strftime('%d %b') for f in (min, max))
        print(f"\n== {sport}: {len(X)} markets {d0} - {d1}, median traded ${st.median(r['vol'] for r in X):.0f} | THE FAVOURITE by its price")
        for lo, hi in bands: show(f"favourite at {lo * 100:.0f}-{hi * 100:.0f}c", [r for r in X if lo <= max(r['p'], 1 - r['p']) < hi])
        show("underdog at 40-50c", [r for r in X if 0.40 <= min(r['p'], 1 - r['p']) < 0.50], False)
        show("underdog at 20-40c", [r for r in X if 0.20 <= min(r['p'], 1 - r['p']) < 0.40], False)
        if sport in ('tennis', 'ALL'):
            print("   favourite at 50-60c, week by week:")
            wk = defaultdict(list)
            for r in X:
                if 0.50 <= max(r['p'], 1 - r['p']) < 0.60: wk[dt.datetime.fromtimestamp(r['s'], dt.UTC).strftime('%G-W%V')].append(r)
            for k in sorted(wk): show(f"  week {k}", wk[k])
