"""Compare entry and exit rules on the candidates saved by live_cands.py (the live bot's own
measurements + the Polymarket quotes after each move). Real-money timing: the buy lands 3.75 s after
the bookmaker move, a sale 1.65 s after it is decided; fees 5 % x p x (1-p) each way.
"""
import pickle, bisect, math, sys, datetime as dt
from collections import Counter, defaultdict

SLIP, CAP, SETTLE, SALE_LAT, STALE = 0.01, 0.02, 1800, 1650, 30 * 60_000
FEE = lambda p: 0.05 * p * (1 - p)
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
WEAK = ('duel', 'olimp', 'betcity', 'linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit')

def load(path):
    return pickle.load(open(path, 'rb'))['cands']

def quote(c, t):
    i = bisect.bisect_right(c['qt'], t)
    if not i or t - c['qt'][i - 1] > STALE: return None
    b, a = c['qb'][i - 1], c['qa'][i - 1]
    if b != b or a != a or not (b > 0.0 and a < 1.0 and a > b): return None
    return (b, a)

def gap_before(c):
    """How far the bookmakers stood above Polymarket before the move, on the side bought."""
    cx = c['cx']
    return (cx['cons_now'] - cx['cons_move'] * c['d'] - c['pre_mid']) * c['d']

def safe(c, mv=0.10, fol=0.05, w=90_000, fs=0.05, sp=0.04, edge=0.04, cons=0.03, follow=0.3, cap=0.90, pause=True, first=False, gap=0.10):
    cx = c['cx']
    return ((gap is None or gap_before(c) < gap) and c['bm'][0] >= mv and max(c['fol']) <= fol and (fs is None or c['fol_start'][w] <= fs) and (not first or c['start'][w] == c['t0'])
            and max(c['sp']) <= sp + 1e-9 and max(c['dec']) <= cap and min(c['edge']) >= edge and not (pause and c['pause'])
            and cx['fam_moved'] >= 2 and cx['cons_move'] >= cons and cx['jumps'] > 0 and cx['follow'] >= follow)

def wider(c, w=90_000, fs=0.05, maxpx=0.40, follow=0.2, weak=True):
    cx = c['cx']
    return (c['bm'][0] >= 0.07 and max(c['fol']) <= 0.10 and (fs is None or c['fol_start'][w] <= fs) and max(c['sp']) <= 0.04 + 1e-9
            and max(c['dec']) <= maxpx and min(c['edge']) >= 0.02 and not (weak and c['book'] in WEAK)
            and cx['fam_moved'] >= 2 and cx['jumps'] > 0 and cx['follow'] >= follow)

def simulate(c, min_edge=0.04, fill_after=3750, give=900, stop=None, catch=1.0, flat=None):
    """One trade as the real bot would do it. stop = sell when the sale price is this far under the buy price."""
    d, t0 = c['d'], c['t0']
    sell = lambda q: q[0] if d > 0 else 1 - q[1]
    buy = lambda q: q[1] if d > 0 else 1 - q[0]
    qd = quote(c, t0 + 1750)
    if not qd: return dict(res='no_live_book')
    best = buy(qd)
    if best + SLIP > c['dec'][1] + CAP: return dict(res='price_ran')
    room = c['edge'][1] - (best + SLIP - c['dec'][1]) - min_edge
    max_px = best + min(max(math.floor(room * 100 + 1e-6) / 100, 0.0), 0.02)
    tb = t0 + fill_after
    qf = quote(c, tb)
    if not qf or buy(qf) > max_px + 1e-9: return dict(res='not_filled')
    entry = buy(qf); cost = entry + FEE(entry)
    need = catch * c['bm'][0] - c['sp'][0] / 2
    give_end = tb + give * 1000
    end = c['qt'][-1] if len(c['qt']) else 0
    t = tb + SETTLE
    while True:
        if t + SALE_LAT > t0 + 3750 + 900_000 + 115_000:
            return dict(res='unfinished', entry=entry)
        q = quote(c, t)
        if not q:
            if t > give_end:
                i = bisect.bisect_right(c['qt'], t)
                b, a = (c['qb'][i - 1], c['qa'][i - 1]) if i else (float('nan'), float('nan'))
                won = True if b == b and b >= 0.97 else False if a == a and a <= 0.03 else None
                if won is None:
                    t += 1000; continue
                pay = 1.0 if (d > 0) == won else 0.0
                return dict(res='game over', entry=entry, exit=pay, ret=(pay - cost) / cost, held=(t - tb) / 1000, tb=tb, tx=t)
            t += 1000; continue
        px = q[0] if d > 0 else q[1]
        caught = (px - c['pre_mid']) * d >= need or sell(q) >= 0.99
        stopped = stop is not None and not caught and sell(q) <= entry - stop
        if not caught and not stopped and t < give_end:
            t += 1000; continue
        q2 = quote(c, t + SALE_LAT)
        if q2 and sell(q2) >= sell(q) - CAP - 1e-9:
            ex = sell(q2)
            return dict(res='caught up' if caught else 'stop' if stopped else 'gave up', entry=entry, exit=ex,
                        ret=(ex - FEE(ex) - cost) / cost, held=(t + SALE_LAT - tb) / 1000, tb=tb, tx=t + SALE_LAT)
        t += 1000

def pick(C, gate, one_per_game=True, lock_ms=120_000, hours=True, **sim):
    """The trades a rule takes, in time order: 2-minute lock per game after a buy, one open position a game."""
    out, seen, open_until = [], {}, {}
    for c in C:
        if hours and not (12 <= U(c['t0']).hour < 21): continue
        if not gate(c): continue
        m = c['m']
        if m in seen and abs(c['t0'] - seen[m]) < lock_ms: continue
        seen[m] = c['t0']
        if one_per_game and open_until.get(m, 0) > c['t0']: continue
        r = simulate(c, **sim)
        if 'ret' not in r: continue
        open_until[m] = r['tx']
        out.append(dict(c=c, **r))
    return out

def line(name, T):
    n = len(T)
    if not n: return f"  {name:46s} n=0"
    r = [t['ret'] for t in T]; w = sum(x > 0 for x in r)
    bal = peak = dd = 0
    for x in r: bal += 12 * x; peak = max(peak, bal); dd = max(dd, peak - bal)
    days = defaultdict(float); cnt = Counter()
    for t in T:
        k = U(t['c']['t0']).strftime('%d'); days[k] += 12 * t['ret']; cnt[k] += 1
    return (f"  {name:46s} n={n:3d} won {w/n*100:3.0f}%  avg {sum(r)/n*100:+5.1f}%  total@$12 {12*sum(r):+7.2f}  drop ${dd:5.1f}  wiped {sum(x <= -0.9 for x in r):2d}  -30..-90%: {sum(-0.9 < x <= -0.3 for x in r):2d}"
            f"  | by day " + ' '.join(f"{k}: {cnt[k]:2d} {days[k]:+6.1f}" for k in ('17', '18', '19')))
