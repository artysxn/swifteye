"""Basketball runs: after N falls of 4c+ in a row (within 15 min, price 25-75c) buy the fallen side.
Honest ranges: games are resampled whole (a game's signals share one result).
usage: runs_an.py <set> [set ...]"""
from sx import *
import sys
sys.argv, SETS = sys.argv[:1], sys.argv[1:]
import importlib.util
G = [g for s in SETS for g in json.load(open(f'price_paths_{s}.json'))]
STEP = 5
def sides(g):
    b, a = g['bid'], g['ask']
    yield 0, b, a, g['win']
    yield 1, [None if x is None else round(1 - x, 3) for x in a], [None if x is None else round(1 - x, 3) for x in b], not g['win']
def jumps(bid, ask, J):
    out = []; last = {1: -99, -1: -99}
    for i in range(2, len(bid)):
        if bid[i] is None or bid[i - 2] is None: continue
        if ask[i] - bid[i] > 0.04 or ask[i - 2] - bid[i - 2] > 0.04: continue
        d = (bid[i] + ask[i]) / 2 - (bid[i - 2] + ask[i - 2]) / 2
        s = 1 if d >= J - 1e-9 else -1 if d <= -J + 1e-9 else 0
        if s and i - last[s] >= 6: out.append((i, s)); last[s] = i
    return out
def events(J, N, lo=0.25, hi=0.75):
    ev = []
    for g in G:
        for k, bid, ask, won in sides(g):
            run = []
            for i, s in jumps(bid, ask, J):
                if s != -1: run = []; continue
                run = [x for x in run if i - x <= 180] + [i]
                m = (bid[i] + ask[i]) / 2
                if len(run) == N and lo <= m <= hi and i + 2 < len(bid) and ask[i + 1] is not None: ev.append((g['id'], k, bid, ask, won, i + 1, (i * STEP - 600) / 60))
                if len(run) >= N: run = run[1:] if N > 1 else []
    return ev
def ret(bid, ask, won, i, hold_s):
    e = ask[i]; cost = e + fee(e)
    if hold_s is None: return ((1.0 if won else 0.0) - cost) / cost
    j = min(len(bid) - 1, i + hold_s // STEP)
    while j > i and bid[j] is None: j -= 1
    if j <= i: return None
    x = bid[j]; return (x - fee(x) - cost) / cost
HOLDS = (('5 min', 300), ('10 min', 600), ('20 min', 1200), ('30 min', 1800), ('to result', None))
print(f"{len(G)} games ({', '.join(SETS)})")
print(f"  {'signal':34s} {'n':>4s} {'games':>5s} {'price':>6s} {'won':>5s} | " + ' | '.join(f"{'sell after ' + h[0] if h[1] else 'hold ' + h[0]:>24s}" for h in HOLDS))
for J, N, first in ((0.04, 1, False), (0.04, 2, False), (0.04, 3, False), (0.04, 4, False), (0.04, 3, True), (0.03, 3, False), (0.02, 3, False), (0.02, 4, False)):
    ev = events(J, N)
    if first:
        seen = set(); ev = [e for e in ev if (e[0], e[1]) not in seen and not seen.add((e[0], e[1]))]
    if len(ev) < 15: print(f"  {N} falls of {J*100:.0f}c+{' (first per side)' if first else ''}: {len(ev)} (too few)"); continue
    by = defaultdict(list)
    for gid, k, bid, ask, won, i, mins in ev: by[gid].append([ret(bid, ask, won, i, h) for _, h in HOLDS] + [ask[i], won])
    rows = [x for v in by.values() for x in v]
    out = f"  {str(N) + ' falls of ' + format(J*100, '.0f') + 'c+' + (' (first per side)' if first else ''):34s} {len(rows):4d} {len(by):5d} {st.mean(x[-2] for x in rows)*100:5.1f}c {sum(x[-1] for x in rows)*100/len(rows):4.0f}% |"
    for k in range(len(HOLDS)):
        v = [x[k] for x in rows if x[k] is not None]
        lo, hi = boot(by, lambda rs, k=k: (st.mean(x[k] for x in rs if x[k] is not None) * 100) if any(x[k] is not None for x in rs) else None, n=1000)
        out += f" {st.mean(v)*100:+6.1f}% ({lo:+5.1f} to {hi:+5.1f}) |"
    print(out)
# when in the game, and how the signal is spread over games
ev = events(0.04, 3)
by = Counter(e[0] for e in ev)
print(f"  3 falls of 4c+: {len(ev)} signals in {len(by)} games; most in one game {max(by.values()) if by else 0}; minute of game (median) {st.median(e[6] for e in ev) if ev else 0:.0f}")
