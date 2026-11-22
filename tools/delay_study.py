"""Does a late Polymarket feed make the snipe win more, and does the DELAY of Polymarket behind the bookmakers
predict a trade? (owner's question, 21 Nov 2026). Uses only the backtest dataset (tools/sweep.py, 17-19 Nov):
for every bookmaker move it holds what Polymarket REALLY showed, by its own clock, 1 to 30 s after the move.

  window  = how long after the bookmaker's move the old Polymarket price was still really there
            (first delay at which the true buy price was more than 2c above the price the bot saw)
  late    = how late our own Polymarket feed was at the decision
  follow / lead = the bot's own per-game history (consensus.rs): of earlier bookmaker jumps in this game, the share
            Polymarket made in the 60 s after them (follow) and in the 40 s up to them (lead)
usage: delay_study.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S
D = S.DELAYS


def window(c):
    """Seconds the old price really stayed: the last delay at which the true buy price was within 2c of the seen one."""
    last = 0.0
    for d, q in zip(D, c['q_d']):
        if not q: return None
        buy = q[1] if c['dir'] > 0 else 1 - q[0]
        if buy - c['px'] > 0.02 + 1e-9: return last
        last = d / 1000
    return 99.0   # still there after 30 s


def line(name, X):
    n = len(X)
    if not n: return f"  {name:58s} {0:5d}"
    w = sum(t['pnl'] > 0 for t in X); p = sum(t['pnl'] for t in X); caught = sum(bool(t['caught']) for t in X)
    return f"  {name:58s} {n:5d} {w * 100 // n:4d}% {p:+9.2f} {p / n:+6.2f} {caught * 100 // n:5d}%"


HDR = f"  {'':58s} {'n':>5s} {'won':>5s} {'profit':>9s} {'each':>6s} {'caught':>6s}"
LOOSE = dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False)

print("== 1. ALL MOVES the loose rules would take: how long the old Polymarket price really stayed, by how late our feed was")
rows = [c for c in S.ROWS if c['q_d'][0] is not None and c['_mv'] >= 0.07]
print(f"  {'feed late by':18s} {'moves':>6s}  old price gone within 1.5 s | 1.5-4 s | 4-10 s | 10-30 s | still there after 30 s")
for nm, lo, hi in (('under 1 s', -1e9, 1000), ('1-5 s', 1000, 5000), ('5-20 s', 5000, 20000), ('20-60 s', 20000, 60000), ('over 60 s', 60000, 1e12)):
    X = [window(c) for c in rows if lo <= c['late_ms'] < hi]; X = [w for w in X if w is not None]
    if not X: continue
    sh = lambda a, b: sum(a <= w < b for w in X) * 100 / len(X)
    print(f"  {nm:18s} {len(X):6d}  {sh(0, 1.5):26.0f}% | {sh(1.5, 4):6.0f}% | {sh(4, 10):5.0f}% | {sh(10, 30):6.0f}% | {sh(30, 1e9):6.0f}%")

for title, p in (("THE BOT'S RULES, as it saw the feed", dict(S.BASE, legacy=True)), ("LOOSER RULES (bigger sample)", LOOSE)):
    T = S.sim(p)
    for t in T: t['w'] = window(t['c'])
    print(f"\n== 2. {title}: trades by how late our feed was at the buy"); print(HDR)
    print(line('all', T))
    for nm, lo, hi in (('feed on time (under 1 s)', -1e9, 1000), ('late 1-5 s', 1000, 5000), ('late 5-20 s', 5000, 20000), ('late 20-60 s', 20000, 60000), ('late over 60 s', 60000, 1e12)):
        print(line(f"  {nm}", [t for t in T if lo <= t['c']['late_ms'] < hi]))
    print(f"   -- the same trades by how long the old price REALLY stayed after the bookmaker's move (the true delay)")
    for nm, lo, hi in (('gone within 5 s (4.2 s is when the bot buys)', 0, 5), ('5-10 s', 5, 10), ('10-30 s', 10, 30), ('still there after 30 s', 30, 1e9)):
        print(line(f"  {nm}", [t for t in T if t['w'] is not None and lo <= t['w'] < hi]))
    print(f"   -- true delay x feed lateness")
    for late in (False, True):
        for nm, lo, hi in (('old price gone within 10 s', 0, 10), ('old price stayed 10 s or more', 10, 1e9)):
            print(line(f"  feed {'late' if late else 'on time'}, {nm}", [t for t in T if t['w'] is not None and (t['c']['late_ms'] > 1000) == late and lo <= t['w'] < hi]))

print("\n== 3. WAIT BEFORE BUYING: buy D seconds after the bookmaker's move instead of 4.2 s, only if the old price is really still there")
for title, p in (("the bot's rules (feed on time: it sees Polymarket's true state)", dict(S.BASE)), ("looser rules", dict(LOOSE, legacy=False))):
    print(f"  -- {title}"); print(HDR)
    print(line('today: buy 4.2 s after the move', S.sim(p)))
    for d in (1000, 2000, 3000, 5000, 7000, 10000, 15000, 30000):
        print(line(f"  buy {d / 1000:.0f} s after the move", S.sim(dict(p, delay=d))))

print("\n== 4. THE BOT'S OWN DELAY HISTORY per game (looser rules, so the rule 'Polymarket lagged earlier' is off)"); print(HDR)
T = S.sim(LOOSE)
print(line('no earlier bookmaker jump in this game to judge by', [t for t in T if t['c']['cx']['jumps'] == 0]))
K = [t for t in T if t['c']['cx']['jumps'] > 0]
print('  -- follow: share of earlier jumps Polymarket made in the 60 s AFTER them (high = it does follow the bookmakers here)')
for lo, hi in ((-9, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 9)): print(line(f"   follow {lo if lo > -9 else 'under'} - {hi if hi < 9 else 'over'}", [t for t in K if lo <= t['c']['cx']['follow'] < hi]))
print('  -- lead: share of earlier jumps Polymarket had ALREADY made in the 40 s before them (high = Polymarket moves first here)')
for lo, hi in ((-9, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 9)): print(line(f"   lead {lo if lo > -9 else 'under'} - {hi if hi < 9 else 'over'}", [t for t in K if lo <= t['c']['cx']['lead'] < hi]))
print('  -- delay = follow minus lead (how much of a jump Polymarket makes AFTER the bookmakers rather than before)')
for lo, hi in ((-9, 0.0), (0.0, 0.2), (0.2, 0.5), (0.5, 9)): print(line(f"   follow - lead {lo if lo > -9 else 'under'} - {hi if hi < 9 else 'over'}", [t for t in K if lo <= t['c']['cx']['follow'] - t['c']['cx']['lead'] < hi]))
print('  -- the same on the bot\'s own rules')
T = S.sim(dict(S.BASE, legacy=True))
for lo, hi in ((-9, 0.2), (0.2, 0.5), (0.5, 9)): print(line(f"   follow - lead {lo if lo > -9 else 'under'} - {hi if hi < 9 else 'over'}", [t for t in T if lo <= t['c']['cx']['follow'] - t['c']['cx']['lead'] < hi]))
