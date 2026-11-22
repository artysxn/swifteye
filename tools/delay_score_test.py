"""Is the bot's per-game delay score real or luck? A test that does not use profit at all.
The score is built from EARLIER bookmaker jumps in the game (follow = share Polymarket made in the 60 s after them,
lead = share it had made in the 40 s before). If it measures something that persists, it must predict what
Polymarket does around the NEXT bookmaker move. Measured on every recorded move (thousands), by Polymarket's own clock:
  before  = how much of the move Polymarket had ALREADY made 1 s after it (it was first, or instant)
  after30 = how much MORE it made between 1 s and 30 s after it (the catch-up the bot is paid for)
One reading per game side per 60 s (many bookmakers report the same event); ranges resample whole games.
usage: delay_score_test.py"""
import sys, os, random, statistics as st
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S
D = S.DELAYS; i1, i30 = D.index(1000), D.index(30000)
rows = []; seen = {}
for c in S.ROWS:
    if c['q_d'][i1] is None or c['q_d'][i30] is None or c['_mv'] < 0.07 or c['pre_mid'] <= 0: continue
    k = (c['match_id'], c['outcome'], c['dir'])
    if k in seen and c['t0'] - seen[k] < 60_000: continue
    seen[k] = c['t0']
    d = c['dir']; mid = lambda q: (q[0] + q[1]) / 2
    before = (mid(c['q_d'][i1]) - c['pre_mid']) * d / c['_mv']
    after = (mid(c['q_d'][i30]) - mid(c['q_d'][i1])) * d / c['_mv']
    cx = c['cx']
    rows.append(dict(m=c['match_id'], day=c['_day'], sport=c['sport'], before=max(-1, min(2, before)), after=max(-1, min(2, after)), jumps=cx['jumps'], follow=cx['follow'], lead=cx['lead'], fl=cx['follow'] - cx['lead']))
K = [r for r in rows if r['jumps'] > 0]
print(f"{len(rows)} separate bookmaker moves of 7+ points in {len({r['m'] for r in rows})} games; {len(K)} with an earlier jump in the game to score by")
random.seed(3)
def rng(X, fn, n=600):
    by = defaultdict(list)
    for r in X: by[r['m']].append(r)
    ks = list(by); v = sorted(fn([y for k in (random.choice(ks) for _ in ks) for y in by[k]]) for _ in range(n))
    return v[int(n * .05)], v[int(n * .95)]
def show(name, X):
    if len(X) < 30: print(f"  {name:34s} {len(X):5d} (too few)"); return
    a = st.mean(r['after'] for r in X); b = st.mean(r['before'] for r in X); big = sum(r['after'] >= 0.3 for r in X) / len(X)
    lo, hi = rng(X, lambda z: st.mean(r['after'] for r in z))
    print(f"  {name:34s} {len(X):5d} moves {len({r['m'] for r in X}):4d} games | already made {b * 100:5.1f}% | caught up 1-30 s after: {a * 100:5.1f}% of the move ({lo * 100:+.0f} to {hi * 100:+.0f}) | caught up 30%+ in {big * 100:4.1f}% of moves")
print("\n== by the delay score (follow - lead) built from EARLIER jumps")
for nm, lo, hi in (('under -1', -99, -1), ('-1 to -0.5', -1, -0.5), ('-0.5 to 0', -0.5, 0), ('0 to 0.2', 0, 0.2), ('0.2 to 0.5', 0.2, 0.5), ('0.5 to 1', 0.5, 1), ('1 or more', 1, 99)):
    show(f"score {nm}", [r for r in K if lo <= r['fl'] < hi])
show('no earlier jump (no score)', [r for r in rows if r['jumps'] == 0])
print("\n== the two parts on their own")
for nm, lo, hi in (('follow under 0.1', -9, 0.1), ('follow 0.1-0.3', 0.1, 0.3), ('follow 0.3-0.6', 0.3, 0.6), ('follow 0.6-1', 0.6, 1.0), ('follow 1+', 1.0, 99)): show(nm, [r for r in K if lo <= r['follow'] < hi])
for nm, lo, hi in (('lead under 0.1', -9, 0.1), ('lead 0.1-0.5', 0.1, 0.5), ('lead 0.5-1', 0.5, 1.0), ('lead 1+', 1.0, 99)): show(nm, [r for r in K if lo <= r['lead'] < hi])
print("\n== does it hold on every day? score 0.2+ against score under 0.2: catch-up 1-30 s after the move")
for d in sorted({r['day'] for r in K}):
    A = [r for r in K if r['day'] == d and r['fl'] >= 0.2]; B = [r for r in K if r['day'] == d and r['fl'] < 0.2]
    if len(A) >= 20 and len(B) >= 20: print(f"  {d} Nov: score 0.2+ {len(A):4d} moves, caught up {st.mean(r['after'] for r in A) * 100:5.1f}% | under 0.2 {len(B):4d} moves, caught up {st.mean(r['after'] for r in B) * 100:5.1f}%")
print("== and in every sport?")
for sp in sorted({r['sport'] for r in K}):
    A = [r for r in K if r['sport'] == sp and r['fl'] >= 0.2]; B = [r for r in K if r['sport'] == sp and r['fl'] < 0.2]
    if len(A) >= 20 and len(B) >= 20: print(f"  {sp:11s} score 0.2+ {len(A):4d} moves, caught up {st.mean(r['after'] for r in A) * 100:5.1f}% | under 0.2 {len(B):4d} moves, caught up {st.mean(r['after'] for r in B) * 100:5.1f}%")
# rank correlation, game-level
import math
x = [r['fl'] for r in K]; y = [r['after'] for r in K]
def rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i]); r = [0] * len(v)
    for k, i in enumerate(o): r[i] = k
    return r
rx, ry = rank(x), rank(y); n = len(x); mx = (n - 1) / 2
rho = sum((a - mx) * (b - mx) for a, b in zip(rx, ry)) / sum((a - mx) ** 2 for a in rx)
print(f"\nrank correlation between the score and the catch-up after the next move: {rho:+.3f} on {n} moves")
