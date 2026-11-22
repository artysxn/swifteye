"""Would an earlier buy make more? Honest version (21 Nov 2026).
The buy now lands about 4.2 s after the bookmaker move: 1.0 s wait + 0.5 s confirmation + about 0.7 s to send
+ about 1.5 s of Polymarket's own delay on live games. Only the first 2.2 s are ours.
For each landing time D after the move: today's rules decide as they do now (the two tests at 1.0 and 1.5 s are kept
for D >= 2.5 s; an earlier landing would mean deciding before the confirmation, which this data cannot replay), the buy
is priced at Polymarket's own-clock quote at D, and the move counts ONLY if the pooled quote stream of other live games
was flowing at THAT moment (no recording gap of 2 s around it). Earlier tests of this skipped that check and were
carried by one buy inside a gap.
usage: buy_sooner.py"""
import sys, os, json, bisect, sqlite3, random, time
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import sweep as S
random.seed(31)
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
CACHE = os.path.expanduser('~/swifteye/research/hole_at.json')
HA = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
allrows = sorted(S.ROWS, key=lambda c: c['t0']); T0 = [c['t0'] for c in allrows]
orig = S.gate; ci = 2
P = dict(S.BASE, legacy=False)
key = lambda c: f"{c['match_id']}|{c['outcome']}|{c['t0']}"
_pool = {}


def pool(c):
    k = key(c)
    if k not in _pool:
        lo, hi = bisect.bisect_left(T0, c['t0'] - 900_000), bisect.bisect_right(T0, c['t0'] + 900_000)
        cnt = defaultdict(int)
        for x in allrows[lo:hi]:
            if x['match_id'] != c['match_id']: cnt[x['match_id']] += 1
        ts = []; used = 0
        for m, _ in sorted(cnt.items(), key=lambda x: -x[1])[:14]:
            o = [r[0] for r in db.execute("SELECT COALESCE(server_ts_ms, ts_ms) FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=?", (m, c['t0'] - 60_000, c['t0'] + 150_000))]
            if len(o) >= 30: ts += o; used += 1
            if used >= 10: break
        time.sleep(0.01)
        _pool[k] = sorted(ts) if used >= 3 else None
    return _pool[k]


def flowing(c, D):
    k = f"{key(c)}|{D}"
    if k not in HA:
        ts = pool(c)
        if ts is None: HA[k] = None
        else:
            t = c['t0'] + D; i = bisect.bisect_right(ts, t)
            HA[k] = (ts[i] - ts[i - 1]) / 1000 if 0 < i < len(ts) else 999.0
    return HA[k] is not None and HA[k] <= 2.0


def run(D):
    def g(c, p, cc):
        if not orig(c, dict(p, clean=False), cc): return False
        return flowing(c, D if D else c['tf'] - c['t0'])
    S.gate = g
    try: return S.sim(dict(P, delay=D) if D else P)
    finally: S.gate = orig


base = run(None)
bk = {(t['c']['match_id'], t['c']['t0']): t for t in base}
print(f"today (buy lands when a real order lands, about 4.2 s after the move): {len(base)} trades, {sum(t['pnl'] > 0 for t in base)} won, ${sum(t['pnl'] for t in base):+.2f}")
print(f"{'buy lands':>12s} {'trades':>7s} {'won':>5s} {'profit':>9s} {'per $':>7s} | {'v today':>8s} (90% range) | same trades: paid on average | trades only the earlier buy gets / only today's buy gets")
for D in (2000, 2500, 3000, 4000, 5000, 7000, 10000):
    tr = run(D); json.dump(HA, open(CACHE, 'w'))
    k2 = {(t['c']['match_id'], t['c']['t0']): t for t in tr}
    by = defaultdict(float)
    for k, t in k2.items(): by[k[0]] += t['pnl']
    for k, t in bk.items(): by[k[0]] -= t['pnl']
    ks = list(by); v = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(1500)) if ks else [0]
    both = [k for k in k2 if k in bk]
    dpx = sum(k2[k]['entry'] - bk[k]['entry'] for k in both) / max(len(both), 1)
    only_new = [k2[k] for k in k2 if k not in bk]; only_old = [bk[k] for k in bk if k not in k2]
    pnl = sum(t['pnl'] for t in tr)
    print(f"{D / 1000:9.1f} s  {len(tr):7d} {100 * sum(t['pnl'] > 0 for t in tr) / max(len(tr), 1):4.0f}% {pnl:+9.2f} {100 * pnl / max(sum(t['spent'] for t in tr), 1):+6.1f}% | {pnl - sum(t['pnl'] for t in base):+8.2f} ({v[75]:+.0f} to {v[len(v) - 76]:+.0f}) | {len(both)} trades, {100 * dpx:+.2f}c | {len(only_new)} ({sum(t['pnl'] for t in only_new):+.2f}) / {len(only_old)} ({sum(t['pnl'] for t in only_old):+.2f})")
# how fast does the price move away in the first seconds? Polymarket's ask on our side at each delay, against the ask at 1 s
print("\nhow the price we would pay moves in the seconds after the bookmaker's move (today's trades, clean ones):")
for D in (1500, 2000, 2500, 3000, 4000, 5000, 7000, 10000, 15000, 30000):
    d = []
    for t in base:
        c = t['c']; q1 = c['q_d'][S.DELAYS.index(1000)]; q = c['q_d'][S.DELAYS.index(D)]
        if q1 and q:
            a1 = q1[1] if c['dir'] > 0 else 1 - q1[0]; a = q[1] if c['dir'] > 0 else 1 - q[0]; d.append(a - a1)
    if d: print(f"   {D / 1000:5.1f} s after the move: {100 * sum(d) / len(d):+.2f}c dearer than at 1 s on average; dearer in {100 * sum(x > 0.001 for x in d) / len(d):.0f}% of trades, by 2c+ in {100 * sum(x >= 0.02 for x in d) / len(d):.0f}%")
