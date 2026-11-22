import sys, statistics as st
sys.path.insert(0, '/Users/danielvinichenko/swifteye-src/tools')
import sweep as S
def line(lab, X, base=None):
    tot = sum(t['pnl'] for t in X)
    print('  %-58s %3d trades | won %3.0f%% | %+7.2f $%s | give-ups %d (%+.2f $)' % (lab, len(X), 100.0 * sum(1 for t in X if t['pnl'] > 0) / max(1, len(X)), tot, '' if base is None else ' (%+.2f v now)' % (tot - base), sum(1 for t in X if not t['caught']), sum(t['pnl'] for t in X if not t['caught'])))
T = S.sim(S.BASE); base = sum(t['pnl'] for t in T)
print("TODAY'S RULES, clean replay:"); line('now', T)
# what Polymarket did before the bookmakers' move, for winners and give-ups
print('\nPolymarket\'s move in the minute before the bookmakers\' move (pm_tr, our way, cents) - caught-up trades v give-ups:')
for lab, X in (('caught up', [t for t in T if t['caught']]), ('gave up', [t for t in T if not t['caught']])):
    v = sorted(100 * t['c']['pm_tr'] for t in X); print('  %-10s median %+5.1fc, all: %s' % (lab, st.median(v), ' '.join('%+.0f' % x for x in v)))
print('\nSKIP THE BUY WHEN POLYMARKET ALREADY MOVED OUR WAY in the 10 minutes before (pm_tr above X):')
for x in (0.01, 0.02, 0.03, 0.05, 0.08):
    R = [c for c in S.ROWS if c['pm_tr'] <= x]
    line('skip if Polymarket moved %d c+ our way first' % round(100 * x), S.sim(S.BASE, rows=R), base)
print('\nSKIP WHEN POLYMARKET MOVED A SHARE OF THE BOOKMAKERS\' MOVE first (pm_tr / move above X):')
for x in (0.25, 0.5, 0.75):
    R = [c for c in S.ROWS if c['pm_tr'] <= x * c['_mv']]
    line('skip if Polymarket made %d%% of the move first' % round(100 * x), S.sim(S.BASE, rows=R), base)

print("\nWHERE THE BOOKMAKERS STOOD AGAINST POLYMARKET just before their move (_gap, points; minus = Polymarket was AHEAD of them):")
for lab, X in (('caught up', [t for t in T if t['caught']]), ('gave up', [t for t in T if not t['caught']])):
    v = sorted(100 * t['c']['_gap'] for t in X if t['c']['_gap'] is not None); print('  %-10s median %+5.1f, all: %s' % (lab, st.median(v), ' '.join('%+.0f' % x for x in v)))
print("\nSKIP THE BUY WHEN POLYMARKET WAS ALREADY AHEAD OF THE BOOKMAKERS before their move (_gap below -X):")
for x in (0.02, 0.03, 0.05, 0.08, 0.10):
    R = [c for c in S.ROWS if c['_gap'] is None or c['_gap'] >= -x]
    line('skip if Polymarket was %d pt+ ahead' % round(100 * x), S.sim(S.BASE, rows=R), base)
