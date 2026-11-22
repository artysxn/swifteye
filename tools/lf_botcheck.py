"""Is the event pricing of lf_model.py fair? The bot's own replay trades (tools/sweep.py, today's rules) found among the
jump events and priced the lf way (bought at the landing, sold at the decision sell price + the jump, else 5 min)."""
import os, sys, bisect, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_patterns as LP
import sweep as S
from lf_model import walk
C = LP.load(); E = [LP.measure(e) for e in LP.events(C)]
idx = collections.defaultdict(list)
for e in E: idx[(e['m'], e['o'])].append(e)
T = S.sim(S.BASE); got = []
for t in T:
    c = t['c']; L = idx.get((c['match_id'], c['outcome'] if c['dir'] > 0 else ('away' if c['outcome'] == 'home' else 'home')), [])
    near = [e for e in L if abs(e['t'] - c['t0']) <= 15_000]
    if not near: continue
    e = min(near, key=lambda e: abs(e['t'] - c['t0'])); w = walk(e)
    if w: got.append((t['pnl'], w[1], w[0], e['size'], c['bm'][1]))
print('bot trades found among the jump events: %d of %d' % (len(got), len(T)))
print('  replay result of those: %+.2f $ (won %d) | priced the lf way: %+.2f $ (won %d, caught %d)' % (sum(g[0] for g in got), sum(1 for g in got if g[0] > 0), sum(g[1] for g in got), sum(1 for g in got if g[1] > 0), sum(g[2] for g in got)))
