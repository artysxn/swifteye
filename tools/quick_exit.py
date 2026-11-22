"""Quick exits (owner, 22 Nov 2026: "50% target, 30 seconds catchup time. if it moves 20% lower at 15 seconds, insta sell").

The replay's buys (tools/sweep.py, clean, 17-20 Nov) are re-sold from Polymarket's recorded book second by second (its own
clock, the bought side's sell price; sales start 2 s after the buy, when bought shares can be sold). Every rule below is
priced the SAME simple way, so they compare with each other: a sale fills at the sell price - 1c (the replay takes 1c
off every sale), fees 0.05 x p x (1 - p) on the buy and the sale, $12 a trade, the replay's own buy price.
  target X%   sell once the sell price reaches Polymarket's price before the move + X% of the bookmakers' move
              - half the spread (as snipe::caught_up)
  give up     sell at that time if the target was not reached
  stop        at 15 s after the buy, sell at once if the sell price is 20% of the bookmakers' move below the buy
              price ('move' reading) or 20% below the buy price itself ('price' reading); 'first 15 s' = checked every
              second during the first 15 s instead of once at 15 s
usage: quick_exit.py      (reads ~/swifteye/oddsfeed.db read-only, by match_id and time) -> ~/swifteye/research/quick_exit.txt
"""
import os, sys, sqlite3, bisect, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
CACHE = {}


def path(c):
    k = (c['match_id'], c['outcome'], c['tf'])
    if k not in CACHE:
        q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND bid > 0 AND ask > 0 AND ts_ms BETWEEN ? AND ? ORDER BY id",
                       (c['match_id'], c['outcome'], c['tf'] - 120_000, c['tf'] + 360_000)).fetchall()
        q.sort()
        CACHE[k] = [(t, b if c['dir'] > 0 else 1 - a, a if c['dir'] > 0 else 1 - b) for t, b, a in q]
    return CACHE[k]


def bid_at(P, ts):
    i = bisect.bisect_right([x[0] for x in P], ts) - 1
    return P[i][1] if i >= 0 else None


def run(t, catch, give_s, stop=None, stop_kind='move', stop_any=False):
    c = t['c']; P = path(c); entry = t['entry']; td = c['tf']
    pre = c['pre_mid'] if c['dir'] > 0 else 1 - c['pre_mid']
    need = pre + catch * c['bm'][1] - c['_sp'] / 2
    stop_px = None if stop is None else (entry - stop * c['bm'][1] if stop_kind == 'move' else entry * (1 - stop))
    ts_ = [x[0] for x in P]; k = bisect.bisect_left(ts_, td + 2000); end = td + give_s * 1000
    while k < len(P) and P[k][0] <= end:
        tt, b, a = P[k]
        if b >= need: return b - 0.01, 'caught', tt
        if stop_px is not None and stop_any and tt <= td + 15_000 and b <= stop_px: return b - 0.01, 'stopped', tt
        k += 1
    if stop_px is not None and not stop_any and give_s > 15:
        b15 = bid_at(P, td + 15_000)
        if b15 is not None and b15 <= stop_px:
            # did it reach the target before 15 s? (then it was already sold above)
            return b15 - 0.01, 'stopped', td + 15_000
    b = bid_at(P, end)
    if b is None: return None
    return b - 0.01, 'gave up', end


RULES = [('now: target 100%, give up at 5 min', dict(catch=1.0, give_s=300)),
         ('target 50%, give up at 5 min', dict(catch=0.5, give_s=300)),
         ('target 100%, give up at 30 s', dict(catch=1.0, give_s=30)),
         ('target 50%, give up at 30 s', dict(catch=0.5, give_s=30)),
         ('target 50%, 30 s, stop 20% of the move at 15 s', dict(catch=0.5, give_s=30, stop=0.2)),
         ('target 50%, 30 s, stop 20% of the price at 15 s', dict(catch=0.5, give_s=30, stop=0.2, stop_kind='price')),
         ('target 50%, 30 s, stop 20% of the move in the first 15 s', dict(catch=0.5, give_s=30, stop=0.2, stop_any=True))]
SETS = [("today's rules", {}), ('no lagged-50% rule', dict(lag=None)), ('no lagged-50%, 7 pt moves', dict(lag=None, min_move=0.07))]
out = ['QUICK EXITS on the replay\'s buys (clean, 17-20 Nov), re-sold from the recorded book second by second; all rules priced the same simple way\n']
for sn, kw in SETS:
    T = S.sim(dict(S.BASE, **kw)); days = sorted({t['c']['_day'] for t in T})
    out.append('%s: %d buys (per day %s Nov)' % (sn, len(T), '/'.join(days)))
    for rn, rk in RULES:
        R = []
        for t in T:
            r = run(t, **rk)
            if r is None: continue
            x, how, _ = r; n = 12.0 / t['entry']; pnl = n * (x - t['entry']) - n * (fee(t['entry']) + fee(max(x, 0.001)))
            R.append((pnl, how, t['c']['_day']))
        w = [p for p, _, _ in R if p > 0]; l = [p for p, _, _ in R if p <= 0]; tot = sum(p for p, _, _ in R)
        by = collections.defaultdict(float)
        for p, _, d in R: by[d] += p
        hows = collections.Counter(h for _, h, _ in R)
        out.append('  %-58s %3d trades | won %3.0f%% | caught %2d, stopped %2d, gave up %2d | win %+5.2f, loss %+5.2f | %+7.2f $ = %+5.1f%% a $ | %s' % (
            rn, len(R), 100.0 * len(w) / max(1, len(R)), hows['caught'], hows['stopped'], hows['gave up'], sum(w) / max(1, len(w)), sum(l) / max(1, len(l)), tot, 100 * tot / (12 * max(1, len(R))),
            ' / '.join('%+.1f' % by[d] for d in days)))
    out.append('')
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/quick_exit.txt'), 'w').write(txt + '\n')
