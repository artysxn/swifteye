"""Hold instead of sell: what if each snipe buy were held to the game's result (no exit spread, no exit fee)?
Backtest trades of 17-19 Nov (tools/sweep.py) and the live real trades since 20 Nov; winners = the side whose
Polymarket bid ended at 97c+ (tools/strategy_tests/games.json). Two-way games only.
usage: hold_test.py"""
import sys, os, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S
W = {g[0]: (g[3], g[4]) for g in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy_tests', 'games.json')))}
def hold(mid, outcome, d, entry, spent):
    if mid not in W or len(W[mid][0]) != 2: return None
    outs, win = W[mid]
    side = outcome if d > 0 else next(o for o in outs if o != outcome)
    n = spent / entry
    return n * ((1.0 if side == win else 0.0) - entry) - n * S.fee(entry)
def line(name, X):
    X = [t for t in X if t['h'] is not None]; n = len(X)
    if not n: return f"  {name:44s} 0"
    a = sum(t['pnl'] for t in X); h = sum(t['h'] for t in X)
    return f"  {name:44s} {n:4d} | sold as now: {sum(t['pnl'] > 0 for t in X) * 100 // n:3d}% won {a:+9.2f} ({a / n:+5.2f} each, worst {min(t['pnl'] for t in X):+6.2f}) | held to result: {sum(t['h'] > 0 for t in X) * 100 // n:3d}% won {h:+9.2f} ({h / n:+5.2f} each, worst {min(t['h'] for t in X):+6.2f})"
LOOSE = dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False)
for title, p in (("THE BOT'S RULES (backtest)", dict(S.BASE)), ("LOOSER RULES (backtest)", LOOSE)):
    T = S.sim(p)
    for t in T: t['h'] = hold(t['c']['match_id'], t['c']['outcome'], t['c']['dir'], t['entry'], t['spent'])
    print(f"\n== {title}: {len(T)} trades, {sum(t['h'] is not None for t in T)} in settled two-way games")
    print(line('all', T)); print(line('  caught up (sold at the target)', [t for t in T if t['caught']])); print(line('  gave up after 5 min', [t for t in T if not t['caught']]))
    print(line('  mixed: sell on catch-up, HOLD the give-ups', [dict(t, h=(t['pnl'] if t['caught'] else t['h'])) for t in T]))
    for lo, hi in ((0, 0.5), (0.5, 0.75), (0.75, 1.0)): print(line(f"  bought at {lo * 100:.0f}-{hi * 100:.0f}c", [t for t in T if lo <= t['entry'] < hi]))
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
L = []
for i, data in db.execute("SELECT id, data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
    v = json.loads(data); n = v['contracts']
    won = None
    r = db.execute("SELECT bid FROM pm_quotes WHERE match_id = ? AND outcome = ? AND bid IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (v['match_id'], v['outcome'])).fetchone()
    if r and (r[0] >= 0.97 or r[0] <= 0.03): won = (r[0] >= 0.97) == (v['dir'] > 0)
    h = None if won is None else n * ((1.0 if won else 0.0) - v['entry']) - v['fee_in']
    L.append(dict(pnl=v['pnl'], h=h, caught=v['reason'] == 'caught up', id=i, home=v['home']))
print(f"\n== LIVE real trades since 20 Nov: {len(L)}, {sum(t['h'] is not None for t in L)} in games that have ended")
print(line('all', L)); print(line('  caught up', [t for t in L if t['caught']])); print(line('  gave up / sold by hand', [t for t in L if not t['caught']]))
print(line('  mixed: sell on catch-up, HOLD the give-ups', [dict(t, h=(t['pnl'] if t['caught'] else t['h'])) for t in L]))
for t in L: print(f"   #{t['id']} {t['home'][:18]:18s} sold {t['pnl']:+6.2f}   held {'?' if t['h'] is None else format(t['h'], '+6.2f')}")

# ---- honest ranges: games resampled whole (a game's trades share one result)
import random
from collections import defaultdict
def rng(T, fn, n=2000, seed=7):
    by = defaultdict(list)
    for t in T: by[t['c']['match_id']].append(t)
    ks = list(by); rnd = random.Random(seed); v = []
    for _ in range(n):
        s = [x for k in (rnd.choice(ks) for _ in ks) for x in by[k]]
        v.append(fn(s))
    v.sort(); return v[int(n * .05)], v[int(n * .95)], sum(x > 0 for x in v) / n
print("\n== RANGES (90%, games resampled whole) and by sport")
for title, p in (("the bot's rules", dict(S.BASE)), ("looser rules", LOOSE)):
    T = [t for t in S.sim(p)]
    for t in T: t['h'] = hold(t['c']['match_id'], t['c']['outcome'], t['c']['dir'], t['entry'], t['spent'])
    T = [t for t in T if t['h'] is not None]
    st_ = sum(t['spent'] for t in T)
    for nm, fn in (('sold as now', lambda s: sum(t['pnl'] for t in s)), ('held to the result', lambda s: sum(t['h'] for t in s)), ('held MINUS sold', lambda s: sum(t['h'] - t['pnl'] for t in s)),
                   ('sell catch-ups, hold give-ups MINUS sold', lambda s: sum((t['pnl'] if t['caught'] else t['h']) - t['pnl'] for t in s))):
        lo, hi, pos = rng(T, fn)
        print(f"  {title:16s} {nm:42s} {fn(T):+9.2f}  range {lo:+8.2f} to {hi:+8.2f}  positive in {pos * 100:.0f}% of resamples   ({len(T)} trades, {len({t['c']['match_id'] for t in T})} games, ${st_:.0f} staked)")
    for sp in sorted({t['c']['sport'] for t in T}):
        X = [t for t in T if t['c']['sport'] == sp]
        print(f"     {sp:11s} {len(X):4d} trades  sold {sum(t['pnl'] for t in X):+8.2f}  held {sum(t['h'] for t in X):+8.2f}  (held: {sum(t['h'] > 0 for t in X) * 100 // len(X)}% won, average price paid {sum(t['entry'] for t in X) / len(X) * 100:.0f}c)")
    # is the side bought underpriced? win rate against the price paid
    for lo, hi in ((0, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.0)):
        X = [t for t in T if lo <= t['entry'] < hi]
        if X: print(f"     paid {lo * 100:.0f}-{hi * 100:.0f}c: {len(X):4d} trades, average price {sum(t['entry'] for t in X) / len(X) * 100:.0f}c, won {sum(t['h'] > 0 for t in X) * 100 / len(X):.0f}%")
