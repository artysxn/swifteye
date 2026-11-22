"""Are pre-match favourites underpriced on Polymarket? On an independent period (tools/prematch_history.mjs).
Each market once: the side priced higher 10 min to 6 h before the listed start, bought 1c above that price, fee
0.05 x p x (1 - p), held to the result.  usage: prematch_an.py <file.ndjson> [more files]"""
import json, sys, random, statistics as st, datetime as dt
fee = lambda a: 0.05 * a * (1 - a)
R = [json.loads(l) for f in sys.argv[1:] for l in open(f) if l.strip()]
R = [r for r in R if 0.02 < r['p'] < 0.98 and abs(r['p'] - 0.5) > 1e-9]
random.seed(8)
def rng(x, n=1000):
    v = sorted(st.mean(random.choice(x) for _ in x) for _ in range(n)); return v[int(n * .05)], v[int(n * .95)]
def side(r, fav=True):
    p = r['p']; f0 = (p > 0.5) == fav
    price = p if f0 else 1 - p; won = (r['w'] == 0) == f0
    return price, int(won)
def show(name, X, fav=True):
    B = [side(r, fav) for r in X]
    if len(B) < 30: print(f"  {name:34s} {len(B):5d} (too few)"); return
    ret = [(w - (p + 0.01) - fee(p + 0.01)) / (p + 0.01 + fee(p + 0.01)) for p, w in B]; lo, hi = rng(ret)
    won = st.mean(w for _, w in B); said = st.mean(p for p, _ in B)
    print(f"  {name:34s} {len(B):5d} markets | price {said * 100:5.1f}c | won {won * 100:5.1f}% (+/-{(won * (1 - won) / len(B)) ** 0.5 * 100:.1f}) | off by {(won - said) * 100:+5.1f} | bought 1c higher, with the fee: {st.mean(ret) * 100:+5.1f}% a $ ({lo * 100:+.0f} to {hi * 100:+.0f})")
print(f"{len(R)} markets, {dt.datetime.fromtimestamp(min(r['s'] for r in R), dt.UTC):%d %b} - {dt.datetime.fromtimestamp(max(r['s'] for r in R), dt.UTC):%d %b %Y}; median traded ${st.median(r['vol'] for r in R):.0f}; price point median {st.median(r['age'] for r in R) / 60:.0f} min before the start")
for title, X in (('ALL', R), ('traded $1,000+', [r for r in R if r['vol'] >= 1000]), ('price point within 2 h of the start', [r for r in R if r['age'] <= 7200])):
    print(f"\n== {title}: THE FAVOURITE, by its price")
    for lo, hi in ((0.50, 0.55), (0.55, 0.60), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.98)):
        show(f"favourite at {lo * 100:.0f}-{hi * 100:.0f}c", [r for r in X if lo <= max(r['p'], 1 - r['p']) < hi])
print("\n== THE UNDERDOG, by its price (all)")
for lo, hi in ((0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50)):
    show(f"underdog at {lo * 100:.0f}-{hi * 100:.0f}c", [r for r in R if lo <= min(r['p'], 1 - r['p']) < hi], False)
print("\n== favourite at 50-60c, week by week")
wk = lambda r: dt.datetime.fromtimestamp(r['s'], dt.UTC).strftime('%G-W%V')
for w in sorted({wk(r) for r in R}): show(f"   {w}", [r for r in R if wk(r) == w and 0.5 <= max(r['p'], 1 - r['p']) < 0.6])
