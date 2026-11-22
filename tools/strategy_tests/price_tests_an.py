"""Pure-price tests on Polymarket (no bookmakers), from price_paths_*.json (price_paths_build.py).

The idea tested (owner, 21 Nov 2026): in a close match, after N falls in a row buy the fallen side and sell at
a slight gain (or cut at a slight loss). And its mirror: after N rises in a row buy the riser.

A FALL of a side = its mid price drops J+ cents within 10 s (one event per 30 s). N in a row = N falls with no
rise of J+ cents between them, all within 15 min. The buy is made 5 s after the Nth fall, at the ask then.
Also the literal bar version: N bars of W seconds in a row, each closing 1+ cent lower.
Costs are real: buy at the ask, sell at the bid, fee 0.05 x p x (1 - p) per share each way.

usage: price_tests_an.py [set ...]   (default: every price_paths_*.json here)
"""
from sx import *
import sys, glob
files = [f'price_paths_{s}.json' for s in sys.argv[1:]] or sorted(glob.glob('price_paths_*.json'))
G = [g for f in files for g in json.load(open(f))]
STEP = 5
print(f"{len(G)} games: {dict(Counter(g['sport'] for g in G))}")


def sides(g):
    """Both sides of the game as (bid, ask, won) on that side's own price scale."""
    b, a = g['bid'], g['ask']
    yield b, a, g['win']
    yield [None if x is None else round(1 - x, 3) for x in a], [None if x is None else round(1 - x, 3) for x in b], not g['win']


def trade(bid, ask, won, i, tp, sl, hold_s):
    """Buy at bar i's ask; sell at the first bid >= entry + tp or <= entry - sl, else after hold_s (None = hold to the result)."""
    e = ask[i]
    if e is None or bid[i] is None: return None
    cost = e + fee(e)
    if hold_s is None: return ((1.0 if won else 0.0) - cost) / cost
    last = min(len(bid) - 1, i + hold_s // STEP); x = None
    for j in range(i + 1, last + 1):
        if bid[j] is None: continue
        x = bid[j]
        if (tp is not None and x >= e + tp - 1e-9) or (sl is not None and x <= e - sl + 1e-9): break
    if x is None: return None
    return (x - fee(x) - cost) / cost


RULES = (('+3c/-3c, 5 min', 0.03, 0.03, 300), ('+5c/-5c, 10 min', 0.05, 0.05, 600), ('+3c, no stop, 5 min', 0.03, None, 300), ('sell after 60 s', None, None, 60), ('sell after 5 min', None, None, 300), ('hold to result', None, None, None))


def study(name, events):
    """events: (game id, sport, bid, ask, won, bar index of the signal, direction +1 = buy that side)."""
    if not events: print(f"  {name:46s} none"); return
    by = defaultdict(list); fw = defaultdict(list)
    for gid, sport, bid, ask, won, i in events:
        r = [trade(bid, ask, won, i + 1, tp, sl, h) for _, tp, sl, h in RULES]
        if r[0] is None: continue
        by[gid].append(r)
        m0 = (bid[i] + ask[i]) / 2
        for h in (30, 60, 300, 600):
            j = i + h // STEP
            if j < len(bid) and bid[j] is not None: fw[h].append((bid[j] + ask[j]) / 2 - m0)
    rows = [x for v in by.values() for x in v]
    if len(rows) < 20: print(f"  {name:46s} {len(rows):5d} (too few)"); return
    out = f"  {name:46s} {len(rows):5d} {len(by):4d} |" + ''.join(f" {st.mean(fw[h]) * 100:+5.2f}" if fw[h] else '   -  ' for h in (30, 60, 300, 600)) + ' |'
    for k in range(len(RULES)):
        v = [x[k] for x in rows if x[k] is not None]
        out += f" {st.mean(v) * 100:+6.1f}%" if v else '    -  '
    lo, hi = boot(by, lambda rs: st.mean(x[0] for x in rs) * 100 if rs else None)
    w = sum(x[0] > 0 for x in rows) / len(rows)
    print(out + f" | rule 1: won {w * 100:3.0f}%, range {lo:+.1f} to {hi:+.1f}")


def jumps(bid, ask, J):
    """Bars where the mid moved J+ within 10 s: [(bar, +1 / -1)], at most one per 30 s per direction."""
    out = []; last = {1: -99, -1: -99}
    for i in range(2, len(bid)):
        if bid[i] is None or bid[i - 2] is None: continue
        if ask[i] - bid[i] > 0.04 or ask[i - 2] - bid[i - 2] > 0.04: continue
        d = (bid[i] + ask[i]) / 2 - (bid[i - 2] + ask[i - 2]) / 2
        s = 1 if d >= J - 1e-9 else -1 if d <= -J + 1e-9 else 0
        if s and i - last[s] >= 6: out.append((i, s)); last[s] = i
    return out


def streak_events(J, N, want, lo, hi, close_only):
    """Signals: the Nth move in a row in direction `want` (-1 = falls: buy the fallen side; +1 = rises: buy the riser)."""
    ev = []
    for g in G:
        for bid, ask, won in sides(g):
            s0 = next(((bid[k] + ask[k]) / 2 for k in range(100, 140) if bid[k] is not None), None)   # price around the start
            if close_only and (s0 is None or not 0.40 <= s0 <= 0.60): continue
            run = []
            for i, s in jumps(bid, ask, J):
                if s != want: run = []; continue
                run = [k for k in run if i - k <= 15 * 60 // STEP] + [i]
                m = (bid[i] + ask[i]) / 2
                if len(run) == N and lo <= m <= hi and i + 2 < len(bid): ev.append((g['id'], g['sport'], bid, ask, won, i))
                if len(run) >= N: run = run[1:] if N > 1 else []
    return ev


def bar_events(W, N, want, lo, hi):
    """Signals: N bars of W seconds in a row, each closing 1+ cent lower (want = -1) or higher (+1)."""
    k = W // STEP; ev = []
    for g in G:
        for bid, ask, won in sides(g):
            n = 0; prev = None
            for i in range(120, len(bid) - 2, k):
                if bid[i] is None or ask[i] - bid[i] > 0.04: n = 0; prev = None; continue
                m = (bid[i] + ask[i]) / 2
                if prev is not None and (m - prev) * want >= 0.01 - 1e-9: n += 1
                else: n = 0
                prev = m
                if n == N and lo <= m <= hi: ev.append((g['id'], g['sport'], bid, ask, won, i)); n = 0
    return ev


def baseline(lo, hi):
    ev = []
    for g in G:
        for bid, ask, won in sides(g):
            for i in range(130, len(bid) - 2, 60):   # every 5 min in play
                if bid[i] is None or ask[i] - bid[i] > 0.04: continue
                if lo <= (bid[i] + ask[i]) / 2 <= hi: ev.append((g['id'], g['sport'], bid, ask, won, i))
    return ev


SETS = sys.argv[1:]
print("\nColumns: signals, games | average move of the mid price afterwards, in cents, in the direction bought | profit per $ under each selling rule:")
print('  rules: ' + ' · '.join(f"({i + 1}) {r[0]}" for i, r in enumerate(RULES)))
for title, lo, hi, close in (("CLOSE MATCHES (started 40-60c), price now 25-75c", 0.25, 0.75, True), ("ANY MATCH, price now 25-75c", 0.25, 0.75, False),
                             ("FAVOURITES: price now 75-97c (a round trip costs about 2%, not 8%)", 0.75, 0.97, False), ("UNDERDOGS: price now 5-25c", 0.05, 0.25, False)):
    print(f"\n== {title}")
    study('baseline: buy every 5 min, no signal', baseline(lo, hi))
    for J in (0.02, 0.04):
        for N in (1, 2, 3, 4):
            study(f"{N} fall{'s' if N > 1 else ''} of {J * 100:.0f}c+ in a row -> buy the fallen side", streak_events(J, N, -1, lo, hi, close))
        for N in (1, 3):
            study(f"{N} rise{'s' if N > 1 else ''} of {J * 100:.0f}c+ in a row -> buy the riser", streak_events(J, N, +1, lo, hi, close))
print("\n== BAR VERSION, any match, price now 25-75c: N bars in a row each 1c+ lower -> buy the fallen side")
for W in (30, 60, 120):
    for N in (3, 4):
        study(f"{N} x {W} s bars down -> buy the fallen side", bar_events(W, N, -1, 0.25, 0.75))
for name, J, N, want, lo, hi in (("3 falls of 2c+ -> buy the fallen side, 25-75c", 0.02, 3, -1, 0.25, 0.75), ("2 falls of 4c+ -> buy the fallen side, 25-75c", 0.04, 2, -1, 0.25, 0.75),
                                 ("1 fall of 4c+ -> buy the fallen FAVOURITE, 75-97c", 0.04, 1, -1, 0.75, 0.97), ("2 falls of 2c+ -> buy the fallen FAVOURITE, 75-97c", 0.02, 2, -1, 0.75, 0.97)):
    print(f"\n== BY SPORT: {name}")
    ev = streak_events(J, N, want, lo, hi, False)
    for sp in sorted({e[1] for e in ev}): study(sp, [e for e in ev if e[1] == sp])
# A side that reaches 98c+: how often does it still lose?
print("\n== NEAR THE END: the first time a side's bid is 98c+ (ask 99c or less), buy and hold to the result")
by = defaultdict(list)
for g in G:
    for bid, ask, won in sides(g):
        i = next((k for k in range(130, len(bid)) if bid[k] is not None and bid[k] >= 0.98 and ask[k] <= 0.99), None)
        if i is None: continue
        e = ask[i]; by[g['sport']].append(((1.0 if won else 0.0) - e - fee(e)) / (e + fee(e)))
for sp, v in sorted(by.items()): print(f"  {sp:12s} {len(v):4d} bets, {sum(x > 0 for x in v)} won, {sum(x < 0 for x in v)} lost, {st.mean(v) * 100:+.2f}% a $")
