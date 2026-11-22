"""Why 262 moves that pass every rule become 36 trades (part of tools/rule_blocks.py's funnel)."""
import os, sys, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S
B = S.BASE; CI = S.CATCH.index(1.0); GI = S.GIVE.index(B['give'])
pas = [c for c in S.ROWS if S.gate(c, B, CI)]
T = S.sim(B); traded = {(t['c']['match_id'], t['c']['t0']) for t in T}
why = collections.Counter(); after_trade = []
recent, busy = {}, {}
first_in_cluster = 0
# replay the lock logic to label each passing move
tr_by_game = collections.defaultdict(list)
for t in T: tr_by_game[t['c']['match_id']].append((t['c']['tf'], t['xts']))
for c in pas:
    m = c['match_id']; k = (m, c['t0'])
    if k in traded: why['TRADED'] += 1; continue
    tf = c['tf']
    near = [x for x in tr_by_game.get(m, []) if abs(tf - x[0]) <= 20_000]
    inside = [x for x in tr_by_game.get(m, []) if x[0] < tf <= x[1]]
    if near: why['same event as a trade (another bookmaker\'s jump within 20 s of it)'] += 1
    elif inside: why['while that game\'s trade was still open'] += 1
    elif c['nofill']: why['no fill (nothing on offer at the price)'] += 1
    else:
        x = c['exits'][CI][GI]; t = S.trade(c, x, B, CI) if x else None
        if t is None: why['the offer was gone when the order would land (2c+ dearer)'] += 1; continue
        # locked by an earlier passing move that was not traded? (120 s lock after any passing move in the game)
        why['within 2 min of an earlier passing move in the game (lock), not traded'] += 1
        after_trade.append(t[0])
for k, v in why.most_common(): print('  %-70s %4d' % (k, v))
if after_trade: print('  the locked ones, as if bought: %d, won %d, %+.2f $' % (len(after_trade), sum(1 for x in after_trade if x > 0), sum(after_trade)))
# how many distinct events (clusters of passing moves within 60 s in a game)
ev = 0; last = {}
for c in sorted(pas, key=lambda c: c['tf']):
    m = c['match_id']
    if m not in last or c['tf'] - last[m] > 60_000: ev += 1
    last[m] = c['tf']
print('  distinct events among the %d passing moves (moves in a game within 60 s of each other = one event): %d' % (len(pas), ev))
