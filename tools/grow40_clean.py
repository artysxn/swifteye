"""What $40 does with ONLY trustworthy trades: the clean replay trades under today's rules (17-20 Nov, quotes flowing
at the buy) plus the real trades since 20 Nov. Stake 25% of the wallet, half in weak games (delay score under 0.2),
never under $5 (a smaller stake is skipped, as the bot does).
  A. the clean replay trades in their real order
  B. a projection: 12 trades a day drawn from those trades, where the 'true' mix of trades is itself uncertain
     (whole games resampled first), so the range covers both luck and not knowing the real edge
usage: grow40_clean.py"""
import sys, os, json, random, sqlite3
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import hole_check2 as H2, feed_lag as FL
T = H2.T; S = T.S
random.seed(40)
fl = lambda c: (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] else None


def clean(c):
    h = H2.H.get(T.key(c)); l = FL.L.get(T.key(c))
    return (h['gap'] <= 2.0) if h else (l is not None and l < 300)


tr = S.sim(dict(S.BASE, legacy=False, clean=False))
rep = [dict(ret=t['pnl'] / t['spent'], weak=(fl(t['c']) is None or fl(t['c']) < 0.2), g='r%s' % t['c']['match_id'], tf=t['c']['tf'], day=t['c']['_day']) for t in tr if clean(t['c'])]
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
live = []
for (data,) in db.execute("SELECT data FROM bot_trades WHERE real=1 AND ts_ms>=1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
    d = json.loads(data); cx = d.get('cx') or {}
    sc = (cx['follow'] - cx['lead']) if cx.get('jumps') else None
    live.append(dict(ret=d['pnl'] / d['cost'], weak=(sc is None or sc < 0.2), g='l%s' % d['match_id']))
pool = rep + live
m = lambda x: sum(t['ret'] for t in x) / len(x)
print(f"trades used: {len(rep)} clean replay ({100 * m(rep):+.1f}% a $ on average, {100 * sum(t['ret'] > 0 for t in rep) / len(rep):.0f}% won) + {len(live)} real ({100 * m(live):+.1f}% a $, {100 * sum(t['ret'] > 0 for t in live) / len(live):.0f}% won) = {len(pool)} ({100 * m(pool):+.1f}% a $, {100 * sum(t['weak'] for t in pool) / len(pool):.0f}% in weak games)")


def step(w, t, pct, cap):
    st = min(pct * w, cap) * (0.5 if t['weak'] else 1.0)
    if st < 5.0 or st > w: return w, 0
    return w + st * t['ret'], 1


print("\nA. the clean replay trades in order, $40 start, 25% (half in weak games)")
for nm, cap in (('ceiling $12 (today)', 12.0), ('no ceiling', 1e9)):
    w = 40.0; lo = 40.0; n = 0; days = {}
    for t in sorted(rep, key=lambda t: t['tf']):
        w, k = step(w, t, 0.25, cap); n += k; lo = min(lo, w); days[t['day']] = w
    print(f"   {nm:20s} $40 -> ${w:.2f} over 3.2 days ({n} of {len(rep)} trades taken; lowest ${lo:.2f}; end of each day " + ' '.join(f"{d}:{v:.1f}" for d, v in sorted(days.items())) + ")")

by = defaultdict(list)
for t in pool: by[t['g']].append(t)
ks = list(by)
print("\nB. projection from $40, 12 trades a day, 25% (half in weak games); 6,000 runs each")
print(f"   {'':34s} {'1 in 10 ends below':>19s} {'middle':>9s} {'1 in 10 ends above':>19s} {'ends under $40':>15s} {'ever under $25':>15s}")
for nm, cap in (('ceiling $12 (today)', 12.0), ('ceiling raised with the wallet', 1e9)):
    for days in (3.2, 7, 30):
        N = int(round(12 * days)); res = []; low25 = 0
        for _ in range(6000):
            truth = [t for k in (random.choice(ks) for _ in ks) for t in by[k]]
            w = 40.0; lo = w
            for _ in range(N):
                w, _k = step(w, random.choice(truth), 0.25, cap); lo = min(lo, w)
            res.append(w); low25 += lo < 25
        res.sort()
        print(f"   {nm:30s} {days:>4} d {res[600]:>18.0f}$ {res[3000]:>8.0f}$ {res[5400]:>18.0f}$ {100 * sum(r < 40 for r in res) / 6000:>14.0f}% {100 * low25 / 6000:>14.0f}%")
# how uncertain the edge itself is
v = sorted(m([t for k in (random.choice(ks) for _ in ks) for t in by[k]]) for _ in range(4000))
print(f"\n   the edge itself (average return per $ staked): {100 * m(pool):+.1f}%, 90% range {100 * v[200]:+.1f}% to {100 * v[3800]:+.1f}%; chance it is below zero {100 * sum(x < 0 for x in v) / 4000:.0f}%")
