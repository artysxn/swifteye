"""Would a rule "do not buy when Polymarket is already where the bookmakers are" help?

Found 20 Nov 2026: a paper buy at 64c after one stale Pinnacle line jumped 35 points to the level the other
bookmakers and Polymarket had reached five minutes earlier. The rules test the trigger bookmaker's CHANGE, not
whether the bookmakers' LEVEL is ahead of Polymarket's price.

Candidates, all computed from what the bot has at the decision (replay measurements of 17-19 Nov, tools/sweep.py):
  shock  = (consensus - Polymarket) now, less its usual size over the 10 min before, in our direction (cx.gap_shock)
  level  = the bookmakers' consensus for the side bought, less the price to pay (raw: includes the bookmakers' margin)
  share  = how much of the trigger bookmaker's move the consensus made too (cons_move / move)
usage: ahead_rule.py
"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S

def feats(t):
    c = t['c']; cx = c['cx']; d = c['dir']
    lvl = cx['cons_now'] if d > 0 else 1 - cx['cons_now']
    return dict(shock=cx['gap_shock'], level=lvl - c['_px'], share=cx['cons_move'] / max(c['_mv'], 1e-9), dir=d, move=c['_mv'], cons=cx['cons_move'])

def line(name, X, base=None):
    n = len(X)
    if not n: return f"  {name:44s} {0:4d}"
    w = sum(t['pnl'] > 0 for t in X); p = sum(t['pnl'] for t in X)
    worst = min(t['pnl'] for t in X); caught = sum(bool(t['caught']) for t in X)
    return f"  {name:44s} {n:4d} {w * 100 // n:4d}% {p:+8.2f} {p / n:+6.2f} {caught * 100 // n:5d}% {worst:+7.2f}" + (f"   kept {n * 100 // len(base)}% of trades, {p - sum(t['pnl'] for t in base):+7.2f} vs today" if base else '')

for title, p in (("THE BOT'S RULES, as it saw the feed (17-19 Nov)", dict(S.BASE, legacy=True)), ("THE BOT'S RULES, feed on time", dict(S.BASE)),
                 ("LOOSER RULES (bigger sample)", dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False))):
    T = S.sim(p)
    for t in T: t['f'] = feats(t)
    print(f"\n== {title}")
    print(f"  {'':44s} {'n':>4s} {'won':>5s} {'profit':>8s} {'each':>6s} {'caught':>6s} {'worst':>7s}")
    print(line('today (no such rule)', T))
    print('  -- shock: the gap to the bookmakers opened beyond its usual size by at least ...')
    for k in (-0.02, 0.0, 0.02, 0.03, 0.04, 0.06):
        print(line(f"   keep shock >= {k * 100:+.0f} pt", [t for t in T if t['f']['shock'] >= k and t['f']['shock'] > -5], T))
    print('  -- level: the bookmakers stand above the price to pay by at least ...')
    for k in (-0.06, -0.03, 0.0, 0.02, 0.04):
        print(line(f"   keep level - price >= {k * 100:+.0f} pt", [t for t in T if t['f']['level'] >= k], T))
    print('  -- share: the consensus made at least this much of the trigger bookmaker\'s move')
    for k in (0.3, 0.5, 0.7):
        print(line(f"   keep share >= {k:.1f}", [t for t in T if t['f']['share'] >= k], T))
    print('  -- which way the trigger moved')
    print(line('   trigger price ROSE (buy that side)', [t for t in T if t['f']['dir'] > 0]))
    print(line('   trigger price FELL (buy the other side)', [t for t in T if t['f']['dir'] < 0]))
    print('  -- the trades each rule would refuse')
    print(line('   shock < 0', [t for t in T if t['f']['shock'] < 0]))
    print(line('   level - price < 0', [t for t in T if t['f']['level'] < 0]))
    print(line('   level - price < 0 AND shock < 2 pt', [t for t in T if t['f']['level'] < 0 and t['f']['shock'] < 0.02]))
    print(line('   share < 0.3', [t for t in T if t['f']['share'] < 0.3]))
