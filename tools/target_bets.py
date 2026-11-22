"""LOWER CATCH-UP TARGET and MORE BETS (owner, 22 Nov 2026: "test if adjusting the target price lower & taking more
bets on waves will generate more profits").

On the clean replay (tools/sweep.py data, 17-20 Nov), with the replay's own fills, fees and timing:
  sale target   the catch-up target at 100% (now), 75% or 50% of the bookmakers' move (the replay holds exits for those)
  buys          decided as now, with the expected gain at the 100% target (min 4c), so a lower target sells the SAME
                buys sooner - or, in the 'gain at the target' rows, the 4c minimum is checked at the lower target
                (fewer buys), or dropped to 2c / 0c (more buys)
  more bets     entry rules loosened step by step (no lagged-50% rule; 7 pt moves; 1 bookmaker family; no weak-game
                cap; no gap rule), and several bets a game (a new move in the same game 30 s after the last one)
Per day as well, because 18 Nov carries most of the replay's profit.
usage: target_bets.py      -> ~/swifteye/research/target_bets.txt
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S


def sim2(p, ci_gate, ci_exit, rows=S.ROWS):
    """S.sim with the buy decided at one catch level and the sale at another."""
    gi = S.GIVE.index(p['give']); lock = p['lock'] * 1000
    out, recent, busy, cnt = [], {}, {}, collections.Counter()
    for c in rows:
        if not S.gate(c, p, ci_gate): continue
        m = c['match_id']; tf = c['tf']
        if lock and m in recent and tf - recent[m] < lock: continue
        recent[m] = tf
        if c['nofill']: continue
        x = c['exits'][ci_exit][gi]
        if not x: continue
        if p['one_per_game'] and busy.get(m, 0) > tf: continue
        if p['max_per_game'] and cnt[m] >= p['max_per_game']: continue
        t = S.trade(c, x, dict(p, catch=S.CATCH[ci_exit]), ci_gate)
        if t is None: continue
        busy[m] = max(busy.get(m, 0), x[0]); cnt[m] += 1
        out.append(dict(c=c, pnl=t[0], caught=x[4], day=c['_day']))
    return out


if __name__ == '__main__':
    ENTRY = [('today\'s rules', {}),
             ('+ no lagged-50% rule', dict(lag=None)),
             ('+ 7 pt moves', dict(lag=None, min_move=0.07)),
             ('+ 1 bookmaker family enough', dict(lag=None, min_move=0.07, fams=1, cons=0.0)),
             ('+ no weak-game cap, no gap rule', dict(lag=None, min_move=0.07, fams=1, cons=0.0, weak_cap=None, gap=None))]
    PERGAME = [('one bet a game at a time', {}), ('several bets a game', dict(one_per_game=False, lock=30))]
    out = []
    days = sorted({c['_day'] for c in S.ROWS})
    out.append('LOWER TARGET x MORE BETS on the clean replay (%s Nov), $12 a bet, the replay\'s own fills and fees. $ total (trades, caught up)' % '-'.join(days))
    out.append('per day: ' + ' / '.join(days) + ' Nov\n')
    best = []
    for en, ekw in ENTRY:
        for pg, pkw in PERGAME:
            out.append('%s, %s' % (en, pg))
            for lab, cig, cie, me in (('target 100% (now)', 2, 2, 0.04), ('target 75%, same buys', 2, 1, 0.04), ('target 50%, same buys', 2, 0, 0.04),
                                     ('target 75%, 4c gain at that target', 1, 1, 0.04), ('target 50%, 4c gain at that target', 0, 0, 0.04),
                                     ('target 50%, 2c gain at that target (more buys)', 0, 0, 0.02), ('target 50%, any gain (most buys)', 0, 0, 0.0)):
                p = dict(S.BASE, **ekw, **pkw, min_edge=me)
                T = sim2(p, cig, cie)
                tot = sum(t['pnl'] for t in T); by = collections.defaultdict(float)
                for t in T: by[t['day']] += t['pnl']
                out.append('   %-46s %+8.2f $ (%3d trades, %3d caught) | %s' % (lab, tot, len(T), sum(1 for t in T if t['caught']), ' / '.join('%+6.1f' % by[d] for d in days)))
                best.append((tot, en, pg, lab, len(T), [by[d] for d in days]))
            out.append('')
    best.sort(key=lambda x: -x[0])
    out.append('BEST 6 by $ total:')
    for b in best[:6]: out.append('   %+8.2f $ %3d trades | %s, %s, %s | by day %s' % (b[0], b[4], b[1], b[2], b[3], ' / '.join('%+.1f' % x for x in b[5])))
    base = [b for b in best if b[1] == "today's rules" and b[2] == 'one bet a game at a time' and b[3] == 'target 100% (now)'][0]
    out.append('   now: %+8.2f $ %d trades | by day %s' % (base[0], base[4], ' / '.join('%+.1f' % x for x in base[5])))
    out.append('settings that beat now: %d of %d; that beat now on every day: %d' % (sum(1 for b in best if b[0] > base[0]), len(best), sum(1 for b in best if all(x >= y for x, y in zip(b[5], base[5])) and b[0] > base[0])))
    txt = '\n'.join(out); print(txt)
    open(os.path.expanduser('~/swifteye/research/target_bets.txt'), 'w').write(txt + '\n')
