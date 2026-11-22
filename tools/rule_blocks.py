"""What does each of the bot's rules block, and what would the blocked trades have made? (owner, 22 Nov 2026: "why is
there so few catchups despite the graphs in theory having multiple? how can we take more bets? if we take 4x more bets
at 60% winrate instead of 1x at 70%, we will make more money")

Clean replay (tools/sweep.py data: every bookmaker move of 7+ pt a loose rule set could take, 17-20 Nov), the replay's
own fills, target 100% of the move, give up at 5 min, $12, with the bot's locks (one bet a game at a time).
  funnel     the moves that pass each rule in turn, in the order sweep.gate checks them
  one rule   today's rules with ONE rule switched off: the extra trades it lets in, their win rate, average win and
             loss, and $
  win v size for every rule set: win rate, average win, average loss, $ a trade - what "more bets at a lower win rate"
             needs to pay
usage: rule_blocks.py      -> ~/swifteye/research/rule_blocks.txt
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

out = []; P = out.append
CI = S.CATCH.index(1.0)
rows = [c for c in S.ROWS if not (c['blind'] and S.CLEAN_DEFAULT)]
B = S.BASE
# ---- funnel in gate order
steps = [('a bookmaker moved 7 pt+ (the replay\'s candidate moves, clean)', lambda c: True),
         ('move 8 pt+', lambda c: c['_mv'] >= B['min_move']),
         ('Polymarket had not followed yet (5% or less)', lambda c: c['_fol'] <= B['max_followed']),
         ('2+ bookmaker families moved, consensus 3 pt+', lambda c: c['cx']['fam_moved'] >= B['fams'] and c['cx']['cons_move'] >= B['cons']),
         ('spread 4c or less', lambda c: c['_sp'] <= B['max_spread'] + 1e-9),
         ('expected gain 4c+ after costs', lambda c: c['_emin'][CI] >= B['min_edge']),
         ('basketball outside the US / Dota spread / weak-game cap', lambda c: not (c['sport'] == 'basketball' and c['_usbb']) and not (c['sport'] == 'dota2' and c['_sp'] > 0.02 + 1e-9) and not (c['_px'] - 0.01 > 0.60 + 1e-9 and (c['cx']['jumps'] == 0 or c['cx']['follow'] - c['cx']['lead'] < 0.2))),
         ('not right after a pause, price 90c or less', lambda c: not c['pause'] and c['_px'] <= B['cap']),
         ('LAGGED: Polymarket followed 50%+ of the earlier jumps', lambda c: c['cx']['jumps'] > 0 and c['cx']['follow'] >= B['lag']),
         ('gap rule (bookmakers not already 10 pt+ ahead before)', lambda c: c['_gap'] is None or c['_gap'] < B['gap']),
         ('Polymarket\'s own clock agrees it had not followed', lambda c: c['_tfol'] is None or c['_tfol'] <= B['max_followed'])]
P('FUNNEL: the replay\'s candidate moves (clean, 17-20 Nov) that pass each rule in turn')
X = rows
for name, f in steps:
    X = [c for c in X if f(c)]
    P('  %-62s %6d moves in %4d games' % (name, len(X), len({c['match_id'] for c in X})))
T = S.sim(B)
P('  %-62s %6d trades in %4d games (the rest: same game busy / locked, or the buy was not really there)\n' % ('TRADED (one bet a game at a time, buy still there)', len(T), len({t['c']['match_id'] for t in T})))


def stats(T):
    w = [t['pnl'] for t in T if t['pnl'] > 0]; l = [t['pnl'] for t in T if t['pnl'] <= 0]
    n = max(1, len(T)); tot = sum(w) + sum(l)
    return len(T), 100.0 * len(w) / n, sum(w) / max(1, len(w)), sum(l) / max(1, len(l)), tot, tot / n


P('TODAY\'S RULES WITH ONE RULE OFF: the trades it adds, and what they make (replay fills, target 100%, 5 min, $12)')
base = stats(T); key = lambda t: (t['c']['match_id'], t['c']['t0'])
bk = {key(t) for t in T}
P('  %-44s %4d trades | won %3.0f%% | win %+5.2f loss %+5.2f | %+8.2f $ = %+5.2f a trade' % ('today\'s rules', *base))
for name, kw in (('move 7 pt+ (not 8)', dict(min_move=0.07)), ('Polymarket followed up to 20% already', dict(max_followed=0.20)),
                 ('1 bookmaker family enough', dict(fams=1, cons=0.0)), ('spread up to 8c', dict(max_spread=0.08)), ('expected gain 2c+ (not 4c)', dict(min_edge=0.02)),
                 ('expected gain any', dict(min_edge=-1.0)), ('US basketball too', dict(bb='all')), ('no Dota spread rule', dict(dota_spread=None)),
                 ('no weak-game cap', dict(weak_cap=None)), ('after a pause too', dict(pause=False)), ('price up to 97c', dict(cap=0.97)),
                 ('NO LAGGED RULE', dict(lag=None)), ('lagged 30% (not 50%)', dict(lag=0.3)), ('no gap rule', dict(gap=None)),
                 ('several bets a game', dict(one_per_game=False, lock=30)), ('the buy may cost 4c more (not 2c)', dict(slip=0.04))):
    U = S.sim(dict(B, **kw)); add = [t for t in U if key(t) not in bk]
    s = stats(U); a = stats(add) if add else (0, 0, 0, 0, 0, 0)
    P('  %-44s %4d trades (%+3d) | won %3.0f%% | %+8.2f $ (%+7.2f v now) || the %3d added: won %3.0f%%, win %+5.2f loss %+5.2f, %+7.2f $' % (name, s[0], s[0] - base[0], s[1], s[4], s[4] - base[4], a[0], a[1], a[2], a[3], a[4]))
P('')
P('WIN RATE v THE SIZE OF WINS AND LOSSES, rule sets from strict to loose (what "4x the bets at 60% won" needs)')
for name, kw in (("today's rules", {}), ('no lagged rule', dict(lag=None)), ('no lagged, 7 pt', dict(lag=None, min_move=0.07)),
                 ('no lagged, 7 pt, 1 family', dict(lag=None, min_move=0.07, fams=1, cons=0.0)), ('loosest', dict(lag=None, min_move=0.07, fams=1, cons=0.0, weak_cap=None, gap=None, min_edge=0.0, max_spread=0.08))):
    s = stats(S.sim(dict(B, **kw)))
    be = -s[3] / (s[2] - s[3]) * 100 if s[2] - s[3] > 0 else float('nan')
    P('  %-30s %4d trades (x%.1f) | won %3.0f%% | average win %+5.2f, average loss %+5.2f -> needs %3.0f%% won to break even | %+8.2f $ = %+5.2f a trade' % (name, s[0], s[0] / base[0], s[1], s[2], s[3], be, s[4], s[5]))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/rule_blocks.txt'), 'w').write(txt + '\n')
