import sys, statistics as st
sys.path.insert(0, '/Users/danielvinichenko/swifteye-src/tools')
import sweep as S
T = S.sim(S.BASE)
def line(lab, X):
    if not X: print('  %-34s   0 trades' % lab); return
    print('  %-34s %3d trades | won %3.0f%% | %+7.2f $ total | %+5.2f $ a trade' % (lab, len(X), 100.0 * sum(1 for t in X if t['pnl'] > 0) / len(X), sum(t['pnl'] for t in X), sum(t['pnl'] for t in X) / len(X)))
print('TODAY\'S RULES on the clean replay (17-19 Nov):'); line('all', T)
print('\nBY POLYMARKET SPREAD AT THE BUY'); 
for lo, hi, lab in ((0, 0.015, '1c'), (0.015, 0.025, '2c'), (0.025, 0.035, '3c'), (0.035, 0.05, '4c')): line(lab, [t for t in T if lo <= t['c']['_sp'] < hi])
print('\nBY THE BOOKMAKERS\' MOVE'); 
for lo, hi, lab in ((0.08, 0.10, '8-10 pt'), (0.10, 0.15, '10-15 pt'), (0.15, 9, '15 pt+')): line(lab, [t for t in T if lo <= t['c']['_mv'] < hi])
print('\nBY HOW THE TRADE ENDED'); line('caught up (sold at the catch-up)', [t for t in T if t['caught']]); line('gave up after 5 min', [t for t in T if not t['caught']])
print('\nBY SPORT')
for s in sorted({t['c']['sport'] for t in T}): line(s, [t for t in T if t['c']['sport'] == s])
print('\nTODAY\'S RULES WITH ONE CHANGE (replay, $ total):')
for lab, kw in (('spread up to 6c (now 4c)', dict(max_spread=0.06)), ('spread up to 8c', dict(max_spread=0.08)), ('spread up to 2c', dict(max_spread=0.02)),
                ('moves 10 pt+ (now 8)', dict(min_move=0.10)), ('moves 6 pt+', dict(min_move=0.06)),
                ('give up after 15 min (now 5)', dict(give=900)), ('give up after 30 min', dict(give=1800)), ('give up after 2 min', dict(give=120)),
                ('sell at 75% of the catch-up (now 100%)', dict(catch=0.75)), ('sell at 50% of the catch-up', dict(catch=0.50)),
                ('stop-loss at 50% of the move', dict(stop=1)), ('sell when the bookmakers give back half', dict(stop=3))):
    X = S.sim(dict(S.BASE, **kw)); line(lab, X)
