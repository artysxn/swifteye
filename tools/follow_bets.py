"""More bets in games that keep catching up (owner, 22 Nov 2026: "games that have one catchup possible should mean the
entire game is possible to catch up ... 2-3 bets per game"). The bot already measures 'this game catches up': the
lagged rule (cx.follow = the share of the bookmakers' earlier jumps in this game that Polymarket followed; today 50%+).
Here: a higher bar, with one bet a game at a time v several (a new move 30 s / 2 min after the last one, up to 2 / 3 a
game). Clean replay 17-20 Nov, its own fills, target 100%, give up 5 min, $12.
usage: follow_bets.py      -> ~/swifteye/research/follow_bets.txt"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S
out = []
days = sorted({c['_day'] for c in S.ROWS})
out.append('LAGGED RULE (Polymarket followed X of the earlier jumps in the game) x BETS A GAME; $12, replay fills. per day %s Nov' % '/'.join(days))
for lag in (0.5, 0.6, 0.7, 0.8, 0.9):
    for lab, kw in (('one at a time (now)', {}), ('several, 2 min apart, up to 3', dict(one_per_game=False, lock=120, max_per_game=3)),
                    ('several, 30 s apart, up to 3', dict(one_per_game=False, lock=30, max_per_game=3)), ('several, 30 s apart, up to 2', dict(one_per_game=False, lock=30, max_per_game=2))):
        T = S.sim(dict(S.BASE, lag=lag, **kw))
        per = collections.Counter(t['c']['match_id'] for t in T); by = collections.defaultdict(float)
        for t in T: by[t['c']['_day']] += t['pnl']
        tot = sum(t['pnl'] for t in T)
        out.append('  followed %2.0f%%+, %-32s %3d bets in %3d games (%d with 2+) | won %3.0f%% | %+7.2f $ = %+5.1f%% a $ | %s' % (
            100 * lag, lab, len(T), len(per), sum(1 for v in per.values() if v >= 2), 100.0 * sum(1 for t in T if t['pnl'] > 0) / max(1, len(T)), tot, 100 * tot / (12 * max(1, len(T))), ' / '.join('%+.1f' % by[d] for d in days)))
    out.append('')
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/follow_bets.txt'), 'w').write(txt + '\n')
