"""Rule variants on CLEAN replay trades only (17-20 Nov): a trade counts when Polymarket quotes were being recorded at
its buy (tools/hole_check2.py). Run as the bot runs: 2-minute lock, one position a game, $12 a trade.
usage: tells_final.py"""
import sys, os, json, random
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import hole_check2 as H2, feed_lag as FL
T = H2.T; S = T.S
random.seed(4)
orig = S.gate
fl = lambda c: (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] else None
G = T.G


def run(p, extra=None):
    S.gate = (lambda c, pp, cc: orig(c, pp, cc) and extra(c)) if extra else orig
    try: tr = S.sim(p)
    finally: S.gate = orig
    for t in tr:
        FL.lag(t['c']); H2.gap(t['c']); t['clean'] = T.clean_now(t['c']) if hasattr(T, 'clean_now') else None
    return tr


def clean(c):
    h = H2.H.get(T.key(c)); l = FL.L.get(T.key(c))
    if h: return h['gap'] <= 2.0
    return (l is not None and l < 300)


def show(name, tr):
    for lab, g in (('clean', [t for t in tr if clean(t['c'])]), ('holes', [t for t in tr if not clean(t['c'])])):
        if not g: print(f"  {name:46s} {lab}: none"); continue
        by = defaultdict(float)
        for t in g: by[t['c']['match_id']] += t['pnl']
        ks = list(by); v = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(1000))
        pnl = sum(t['pnl'] for t in g); sp = sum(t['spent'] for t in g)
        days = ' '.join(f"{d}:{sum(t['pnl'] for t in g if t['c']['_day'] == d):+6.1f}({sum(1 for t in g if t['c']['_day'] == d)})" for d in ('17', '18', '19', '20'))
        worst = min(t['pnl'] for t in g)
        print(f"  {name if lab == 'clean' else '':46s} {lab}: {len(g):3d} trades, {100 * sum(t['pnl'] > 0 for t in g) / len(g):3.0f}% won, ${pnl:+7.2f} ({100 * pnl / sp:+5.1f}% a $; 90% range {v[50]:+.0f} to {v[950]:+.0f}), worst {worst:+.2f} | by day {days}")


B = dict(S.BASE, legacy=False, clean=False)      # split into clean and holes below
qu = lambda c: (G.get(T.key(c)) or {}).get('q_upd')
V = [
    ("today's rules", B, None),
    ("today + CS2 from 8 pt (the paper trial)", dict(B, min_move=0.08), lambda c: c['_mv'] >= 0.10 or c['sport'] == 'cs2'),
    ("today, without Dota 2", dict(B, skip=('dota2',)), None),
    ("today, CS2 only", B, lambda c: c['sport'] == 'cs2'),
    ("today, tennis only", B, lambda c: c['sport'] == 'tennis'),
    ("today, Dota 2 only", B, lambda c: c['sport'] == 'dota2'),
    ("move 7+ in every sport", dict(B, min_move=0.07), None),
    ("move 7 to 10 only", dict(B, min_move=0.07), lambda c: c['_mv'] < 0.10),
    ("today + no Polymarket re-quote before the buy", B, lambda c: qu(c) == 0),
    ("today + Polymarket DID re-quote before the buy", B, lambda c: qu(c) is not None and qu(c) > 0),
    ("today + half or more of the bookmaker groups moved", B, lambda c: c['cx']['fam_moved'] / max(c['cx']['fams'], 1) >= 0.5),
    ("today + spread 2c or less", dict(B, max_spread=0.02), None),
    ("today + delay score 0.2+", B, lambda c: fl(c) is not None and fl(c) >= 0.2),
    ("today + delay score under 0.2", B, lambda c: fl(c) is None or fl(c) < 0.2),
    ("today + follow 50%+", dict(B, lag=0.5), None),
]
print("CLEAN = Polymarket quotes were being recorded at the buy; HOLES = the buy sits where the recording had no quotes (not real)")
for name, p, extra in V:
    show(name, run(p, extra))
json.dump(H2.H, open(H2.CACHE, 'w')); json.dump(FL.L, open(FL.CACHE, 'w'))
