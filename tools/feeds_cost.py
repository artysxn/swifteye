# What do the feed changes of 20-21 Nov cost in trades? Replay of 17-20 Nov under today's rules.
#  paused: 22bet 22bit 1xbit betwinner megapari pari paf (+ small football feeds, kalshi)
#  slowed: fonbet olimp betcity 1 s -> 2 s; duel m88 1.5 s -> 3 s (pinnacle 2 s -> 20 s)
# A slowed feed shows a move later. Modelled as: its move reaches the bot 1 s later (what Polymarket had really
# done is read 2.5 s after the move instead of 1.5 s, and the order lands 5 s after it instead of 4.2 s).
import sys, os, collections, time, copy
sys.path.insert(0, os.path.expanduser('~/swifteye-src/tools'))
sys.argv = ['sweep.py', 'none']
import sweep as S
PAUSED = {'kalshi', '22bet', '22bit', '1xbit', 'betwinner', 'megapari', 'paf', 'novibet', 'betika', 'betking', 'betpawa', 'sportybet', 'vinn', 'pari'}
SLOW = {'fonbet', 'olimp', 'betcity', 'duel', 'm88', 'pinnacle'}
D = S.DELAYS
def shifted(c, d_test, d_buy):
    n = dict(c)
    q = c['q_d'][D.index(d_test)] if c.get('q_d') else None
    n['_tfol'] = ((q[0] + q[1]) / 2 - c['pre_mid']) * c['dir'] / c['bm'][1] if q else c['_tfol']
    n['q_tf'] = c['q_d'][D.index(d_buy)] if c.get('q_d') else c['q_tf']
    return n
def tot(T): return sum(t['pnl'] for t in T)
def show(name, T, T0=None):
    print('%-66s trades %3d | won %2d | $%+7.2f%s | by sport %s' % (name, len(T), sum(1 for t in T if t['pnl'] > 0), tot(T), (' (%+d trades, $%+.2f)' % (len(T) - len(T0), tot(T) - tot(T0))) if T0 is not None else '', dict(collections.Counter(t['c']['sport'] for t in T))))
rows = S.ROWS
for clean in (False, True):
    P = dict(S.BASE, clean=clean)
    print('\n=== %s' % ('only moves outside gaps in the recording (honest set)' if clean else 'all recorded moves'))
    T0 = S.sim(P); show('all feeds at full speed (as on 17-20 Nov)', T0)
    T1 = S.sim(P, [c for c in rows if c['trigger'] not in PAUSED]); show('feeds paused', T1, T0)
    T2 = S.sim(P, [shifted(c, 2500, 5000) if c['trigger'] in SLOW else c for c in rows]); show('feeds slowed (each slowed move 1 s later)', T2, T0)
    T3 = S.sim(P, [shifted(c, 2500, 5000) if c['trigger'] in SLOW else c for c in rows if c['trigger'] not in PAUSED]); show('BOTH: paused + slowed 1 s (today)', T3, T0)
    T4 = S.sim(P, [shifted(c, 3000, 7000) if c['trigger'] in SLOW else c for c in rows if c['trigger'] not in PAUSED]); show('both, slowed moves 1.5 to 2.8 s later (worst case)', T4, T0)
    T5 = S.sim(P, [shifted(c, 2500, 5000) for c in rows if c['trigger'] not in PAUSED]); show('for scale: EVERY feed 1 s later', T5, T0)
    T6 = S.sim(P, [shifted(c, 3000, 7000) for c in rows]); show('for scale: every feed 1.5 to 2.8 s later', T6, T0)
