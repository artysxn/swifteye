"""2-3 BETS A GAME (owner, 22 Nov 2026: "games that have one catchup possible should mean the entire game is possible to
catch up. investigate if there is any way to do 2-3 bets per game instead of 1 at the right timings").

Patterns first, on the clean replay (tools/sweep.py data, 17-20 Nov, the replay's own fills and exits: target 100% of
the move, give up at 5 min, $12): every LATER move in a game, as if bought, split by how the game's first trade under
today's rules ended - caught up, gave up, or no trade in that game - and by how long after that trade's sale it came.
'Later moves' pass a looser gate (two levels: no lagged-50% rule; and the loosest: also 7 pt moves, one bookmaker family,
no weak-game cap, no gap rule) so there is something to count.
Then the bets: the first bet in a game by today's rules; after a CAUGHT-UP trade in that game, more bets (up to 3 a game)
by a looser gate, each only after waiting W seconds from the last sale; after a give-up, no more bets in that game (or
today's rules only).
usage: game_bets.py      -> ~/swifteye/research/game_bets.txt
"""
import os, sys, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

CI = S.CATCH.index(1.0); GI = S.GIVE.index(300)
P0 = dict(S.BASE)
LOOSE1 = dict(S.BASE, lag=None)
LOOSE2 = dict(S.BASE, lag=None, min_move=0.07, fams=1, cons=0.0, weak_cap=None, gap=None)


def taken(c, p):
    """As if bought now: (pnl, caught, sale time) or None."""
    if c['nofill'] or not S.gate(c, p, CI): return None
    x = c['exits'][CI][GI]
    if not x: return None
    t = S.trade(c, x, p, CI)
    return None if t is None else (t[0], x[4], x[0])


if __name__ == '__main__':
    out = []
    P = lambda s: out.append(s)
    # ---- patterns: the first today's-rules trade of each game, and every later move
    first = {}
    for t in S.sim(P0):
        m = t['c']['match_id']
        if m not in first: first[m] = (t['c']['tf'], t['xts'], t['caught'], t['pnl'])
    P('PATTERNS: later moves in a game, as if bought (replay fills, target 100%%, give up 5 min, $12), by how the game\'s first trade under today\'s rules ended. %d games had such a trade (%d caught up, %d gave up)\n' % (
        len(first), sum(1 for v in first.values() if v[2]), sum(1 for v in first.values() if not v[2])))
    for gname, G in (('no lagged-50% rule', LOOSE1), ('loosest (also 7 pt, 1 family, no weak cap, no gap rule)', LOOSE2)):
        P('  later moves passing: %s' % gname)
        groups = collections.defaultdict(list)
        for c in S.ROWS:
            r = taken(c, G)
            if r is None: continue
            m = c['match_id']; f = first.get(m)
            if f is None: groups['games with no trade under today\'s rules'].append(r); continue
            if c['tf'] <= f[1]: continue                       # before or during the first trade
            gap_s = (c['tf'] - f[1]) / 1000
            key = ('after a CATCH-UP' if f[2] else 'after a GIVE-UP') + (', within 5 min of its sale' if gap_s < 300 else (', 5-30 min after' if gap_s < 1800 else ', 30 min+ after'))
            groups[key].append(r)
        for k in sorted(groups):
            X = groups[k]
            P('    %-58s %4d moves | caught up %3.0f%% | won %3.0f%% | %+6.2f $ a move | %+8.2f $ total' % (k, len(X), 100.0 * sum(1 for r in X if r[1]) / len(X), 100.0 * sum(1 for r in X if r[0] > 0) / len(X), st.mean(r[0] for r in X), sum(r[0] for r in X)))
        P('')


    def sim3(G2, wait_s, max_n=3, after_giveup='stop'):
        """First bet by today's rules; after a caught-up bet, more (up to max_n) by G2, wait_s after the last sale."""
        state = {}            # match -> [bets, last sale time, last caught, any give-up]
        res = []
        for c in S.ROWS:
            m = c['match_id']; s = state.get(m)
            if s is None:
                r = taken(c, P0)
                if r is None: continue
                state[m] = [1, r[2], r[1], not r[1]]; res.append((r[0], r[1], 1, c['_day'])); continue
            n, last_sale, last_caught, any_gu = s
            if n >= max_n or c['tf'] < last_sale + wait_s * 1000: continue
            if any_gu and after_giveup == 'stop': continue
            gate = G2 if (last_caught and not any_gu) else P0
            r = taken(c, gate)
            if r is None: continue
            s[0] += 1; s[1] = r[2]; s[2] = r[1]; s[3] = s[3] or not r[1]
            res.append((r[0], r[1], s[0], c['_day']))
        return res


    P('BETS: the first in a game by today\'s rules; after a caught-up bet, up to 3 a game by a looser gate after waiting W s from the last sale; after a give-up no more in that game')
    base = sim3(P0, 0, max_n=1)
    days = sorted({r[3] for r in base})
    def line(lab, R):
        tot = sum(r[0] for r in R); by = collections.defaultdict(float)
        for r in R: by[r[3]] += r[0]
        extra = [r for r in R if r[2] > 1]
        P('  %-62s %3d bets (%2d 2nd/3rd) | won %3.0f%% | %+7.2f $ = %+5.1f%% a $ | 2nd/3rd bets %+7.2f $ | %s' % (lab, len(R), len(extra), 100.0 * sum(1 for r in R if r[0] > 0) / max(1, len(R)), tot, 100 * tot / (12 * max(1, len(R))), sum(r[0] for r in extra), ' / '.join('%+.1f' % by[d] for d in days)))
    line('one bet a game (now)', base)
    for gname, G in (("today's rules", P0), ('no lagged-50% rule', LOOSE1), ('loosest', LOOSE2)):
        for w in (0, 60, 300, 900):
            line('2nd/3rd by %s, wait %d s' % (gname, w), sim3(G, w))
    line('2nd/3rd by today\'s rules, wait 60 s, also after a give-up', sim3(P0, 60, after_giveup='go'))
    txt = '\n'.join(out); print(txt)
    open(os.path.expanduser('~/swifteye/research/game_bets.txt'), 'w').write(txt + '\n')
