import sys
sys.path.insert(0, '/Users/danielvinichenko/swifteye-src/tools')
import sweep as S
A = S.sim(S.BASE); B = S.sim(dict(S.BASE, give=1800))
key = lambda t: (t['c']['match_id'], t['c']['t0'])
b = {key(t): t for t in B}
print('the give-ups of today\'s rules (5 min), and the same buys given 30 minutes:')
tot5 = tot30 = 0
for t in A:
    if t['caught']: continue
    u = b.get(key(t))
    tot5 += t['pnl']; tot30 += u['pnl'] if u else 0
    print('  %-10s bought %4.1fc | 5 min: sold %4.1fc %+6.2f $ | 30 min: %s' % (t['c']['sport'], 100 * t['entry'], 100 * t['exit'], t['pnl'],
          ('caught up, sold %4.1fc %+6.2f $' % (100 * u['exit'], u['pnl'])) if u and u['caught'] else (('still not, sold %4.1fc %+6.2f $' % (100 * u['exit'], u['pnl'])) if u else 'not taken')))
print('give-ups total: 5 min %+.2f $, 30 min %+.2f $' % (tot5, tot30))
