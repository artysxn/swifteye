"""A blended setup: keep (and add) trades, but size each by its quality instead of skipping it.
Backtest 17-20 Nov with the cached signals; a trade's result scales with its stake. Stakes as shares of the full stake.
  score = the bot's own per-game delay score (follow - lead); 'strong' = 0.2 or more
usage: blend.py"""
import sys, os, json, random, sqlite3, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
src = open(os.path.join(HERE, 'systems_backtest.py')).read().split("HDR = f\"")[0]
exec(compile(src, 'systems_backtest_head', 'exec'))
random.seed(41)
SPAN = (S.ROWS[-1]['tf'] - S.ROWS[0]['tf']) / 86_400_000
feat = lambda t: F[key(t['c'])]
strong = lambda t: feat(t)['fl'] is not None and feat(t)['fl'] >= 0.2
big = lambda t: t['c']['_mv'] >= 0.10
B0 = run(BASE); M8 = run(dict(BASE, min_move=0.08))
CS8 = [t for t in M8 if big(t) or t['c']['sport'] == 'cs2']          # 8 points for CS2 only (approximation: the 8-point run, tennis/others under 10 dropped)


def weigh(T, w):
    out = []
    for t in T:
        x = w(t)
        if x > 0: out.append(dict(t, pnl=t['pnl'] * x, spent=t['spent'] * x, w=x))
    return out


def stat(T, base):
    T = sorted(T, key=lambda t: t['c']['tf']); cum = peak = dd = 0.0
    for t in T:
        cum += t['pnl']; peak = max(peak, cum); dd = max(dd, peak - cum)
    p = sum(t['pnl'] for t in T); lost = -sum(t['pnl'] for t in T if t['pnl'] < 0); staked = sum(t['spent'] for t in T)
    ex18 = sum(t['pnl'] for t in T if t['c']['_day'] != '18')
    by = defaultdict(float); bb = defaultdict(float)
    for t in T: by[t['c']['match_id']] += t['pnl']
    for t in base: bb[t['c']['match_id']] += t['pnl']
    games = list(set(by) | set(bb)); better = 0; dds = []
    for _ in range(1500):
        s = [random.choice(games) for _ in games]
        better += sum(by.get(g, 0) for g in s) > sum(bb.get(g, 0) for g in s)
    # usual biggest drop in a month, whole games drawn
    gl = defaultdict(list)
    for t in T: gl[t['c']['match_id']].append(t['pnl'])
    gl = list(gl.values()); n_month = max(1, round(len(T) / SPAN * 30))
    for _ in range(1500):
        seq = []
        while len(seq) < n_month: seq += random.choice(gl)
        c = pk = d = 0.0
        for x in seq[:n_month]:
            c += x; pk = max(pk, c); d = max(d, pk - c)
        dds.append(d)
    dds.sort()
    return dict(n=len(T), won=sum(t['pnl'] > 0 for t in T) / len(T), p=p, lost=lost, staked=staked, roi=p / staked, dd=dd, ex18=ex18, better=better / 1500, mdd=dds[750], worst=min(t['pnl'] for t in T))


SETUPS = (
    ('the bot as it is ($12 every trade)', B0, lambda t: 1.0),
    ('as it is, but skip weak games (the rule)', B0, lambda t: 1.0 if strong(t) else 0.0),
    ('as it is, HALF stake in weak games', B0, lambda t: 1.0 if strong(t) else 0.5),
    ('move 8+, $12 every trade', M8, lambda t: 1.0),
    ('move 8+, half stake in weak games', M8, lambda t: 1.0 if strong(t) else 0.5),
    ('move 8+, half in weak games AND on 8-10 pt moves', M8, lambda t: (1.0 if strong(t) else 0.5) * (1.0 if big(t) else 0.5)),
    ('move 8+ for CS2 only, half in weak games', CS8, lambda t: 1.0 if strong(t) else 0.5),
    ('move 8+ for CS2 only, half in weak, half on 8-10', CS8, lambda t: (1.0 if strong(t) else 0.5) * (1.0 if big(t) else 0.5)),
    ('move 8+, full only if strong AND 10+, else half', M8, lambda t: 1.0 if (strong(t) and big(t)) else 0.5),
    ('move 8+, weak games at a third', M8, lambda t: 1.0 if strong(t) else 1 / 3),
)
print(f"{'setup':52s} {'trades':>6s} {'won':>4s} {'profit':>8s} {'lost':>6s} {'staked':>7s} {'per $':>6s} {'drop':>6s} {'worst':>6s} {'month drop':>10s} {'w/o 18 Nov':>10s} {'beats now':>9s}")
base = weigh(B0, lambda t: 1.0)
for name, T, w in SETUPS:
    X = weigh(T, w); s = stat(X, base)
    print(f"{name:52s} {s['n']:6d} {s['won'] * 100:3.0f}% {s['p']:+8.2f} {s['lost']:6.2f} {s['staked']:7.0f} {s['roi'] * 100:+5.1f}% {s['dd']:6.2f} {s['worst']:+6.2f} {s['mdd']:10.0f} {s['ex18']:+10.2f} {s['better'] * 100:8.0f}%")
# the live trades under the sizing rule (all were 10+ point moves)
db2 = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
L = [json.loads(d) for (d,) in db2.execute("SELECT data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms")]
for nm, w in (('as traded ($12 each)', lambda v: 1.0), ('skip weak games', lambda v: 1.0 if v['cx']['follow'] - v['cx']['lead'] >= 0.2 else 0.0), ('half stake in weak games', lambda v: 1.0 if v['cx']['follow'] - v['cx']['lead'] >= 0.2 else 0.5)):
    X = [(v['pnl'] * w(v), v['cost'] * w(v)) for v in L if w(v) > 0]
    print(f"LIVE 14 real trades, {nm:26s}: {len(X):2d} trades, profit {sum(a for a, _ in X):+6.2f}, lost {-sum(a for a, _ in X if a < 0):5.2f}, staked {sum(b for _, b in X):6.0f}, per $ {sum(a for a, _ in X) / sum(b for _, b in X) * 100:+5.1f}%")
