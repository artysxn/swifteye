"""WIDEN THE BOT'S RULES, one at a time and in pairs (owner, 22 Nov 2026: "for the bot ... it's too narrow. consider
removing one of the rules to allow more and see what happens (or adjusting instead of removing)").

Clean replay (tools/sweep.py data: bookmaker moves of 7 pt+ that a loose rule set could take, 17-20 Nov; nothing smaller
or looser exists in it), the replay's own fills and exits (target 100% of the move, give up at 5 min), $12, the bot's
locks. Each rule is moved across its range with every other rule as today; per row: trades, win rate, average win and
loss, total $, $ a trade, $ per day. 'Stake to match' = how much bigger each bet would have to be for the same total as
today (the owner's rule: same money, more trades -> a lower stake per trade, a higher win rate -> a higher stake).
usage: rule_adjust.py      -> ~/swifteye/research/rule_adjust.txt
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

B = S.BASE
days = sorted({c['_day'] for c in S.ROWS})
out = []; P = out.append


def row(lab, kw, base=None, mark=''):
    T = S.sim(dict(B, **kw))
    w = [t['pnl'] for t in T if t['pnl'] > 0]; l = [t['pnl'] for t in T if t['pnl'] <= 0]; n = len(T); tot = sum(w) + sum(l)
    by = collections.defaultdict(float)
    for t in T: by[t['c']['_day']] += t['pnl']
    neg = sum(1 for d in days if by[d] < 0)
    P('  %1s %-40s %4d trades | won %3.0f%% | win %+5.2f loss %+5.2f | %+8.2f $ | %+5.2f a trade | days below 0: %d | %s' % (
        mark, lab, n, 100.0 * len(w) / max(1, n), sum(w) / max(1, len(w)), sum(l) / max(1, len(l)), tot, tot / max(1, n), neg, ' / '.join('%+6.1f' % by[d] for d in days)))
    return n, tot


P('WIDEN THE RULES: clean replay %s Nov, $12, target 100%%, give up at 5 min. Per day: %s Nov. ">" = today\'s setting\n' % ('-'.join(days), ' / '.join(days)))
base = row("TODAY'S RULES", {}, mark='>')
GRID = [
    ('MOVE SIZE (bookmaker move, pt; data starts at 7)', 'min_move', [(0.07, '7 pt'), (0.08, '8 pt'), (0.09, '9 pt'), (0.10, '10 pt'), (0.12, '12 pt')]),
    ('POLYMARKET ALREADY FOLLOWED (max share of the move)', 'max_followed', [(0.0, '0%'), (0.05, '5%'), (0.10, '10%'), (0.15, '15%'), (0.20, '20%'), (0.30, '30%')]),
    ('BOOKMAKER FAMILIES THAT MOVED', 'fams', [(1, '1 family'), (2, '2 families'), (3, '3 families')]),
    ('CONSENSUS MOVE (pt)', 'cons', [(0.0, 'any'), (0.02, '2 pt'), (0.03, '3 pt'), (0.05, '5 pt')]),
    ('SPREAD AT THE BUY (max, c)', 'max_spread', [(0.01, '1c'), (0.02, '2c'), (0.03, '3c'), (0.04, '4c'), (0.06, '6c'), (0.08, '8c')]),
    ('EXPECTED GAIN AFTER COSTS (min, c)', 'min_edge', [(-1.0, 'any'), (0.0, '0c'), (0.01, '1c'), (0.02, '2c'), (0.03, '3c'), (0.04, '4c'), (0.05, '5c'), (0.06, '6c')]),
    ('LAGGED (Polymarket followed X of the earlier jumps)', 'lag', [(None, 'off'), (0.2, '20%'), (0.3, '30%'), (0.4, '40%'), (0.5, '50%'), (0.6, '60%')]),
    ('WEAK-GAME CAP (no weak game above X)', 'weak_cap', [(None, 'off'), (0.5, '50c'), (0.6, '60c'), (0.7, '70c'), (0.8, '80c')]),
    ('GAP RULE (skip when bookmakers already X ahead before)', 'gap', [(None, 'off'), (0.05, '5 pt'), (0.10, '10 pt'), (0.15, '15 pt'), (0.20, '20 pt')]),
    ('PRICE CAP', 'cap', [(0.80, '80c'), (0.85, '85c'), (0.90, '90c'), (0.95, '95c')]),
    ('BASKETBALL', 'bb', [('none', 'none'), ('nonus', 'outside the US'), ('all', 'all')]),
    ('JUMP RIGHT AFTER A PAUSE', 'pause', [(True, 'skipped'), (False, 'allowed')]),
    ('THE BUY MAY COST MORE THAN THE DECISION PRICE (max, c)', 'slip', [(0.01, '1c'), (0.02, '2c'), (0.03, '3c'), (0.05, '5c')]),
]
for title, key, vals in GRID:
    P('\n' + title)
    for v, lab in vals:
        row(lab, {key: v}, mark='>' if B[key] == v else '')
txt = '\n'.join(out)
open(os.path.expanduser('~/swifteye/research/rule_adjust.txt'), 'w').write(txt + '\n')
print(txt)

# ---- combinations of the loosenings that cost (almost) nothing alone
import itertools
LOOSE = [('expected gain 0c', dict(min_edge=0.0)), ('followed up to 10%', dict(max_followed=0.10)), ('spread up to 6c', dict(max_spread=0.06)),
         ('weak cap 70c', dict(weak_cap=0.70)), ('lagged 40%', dict(lag=0.4)), ('moves 7 pt', dict(min_move=0.07))]
res = []
for k in range(1, len(LOOSE) + 1):
    for combo in itertools.combinations(LOOSE, k):
        kw = {}
        for _, d in combo: kw.update(d)
        T = S.sim(dict(B, **kw)); tot = sum(t['pnl'] for t in T)
        by = collections.defaultdict(float)
        for t in T: by[t['c']['_day']] += t['pnl']
        res.append((tot, len(T), ' + '.join(n for n, _ in combo), kw, [by[d] for d in days], 100.0 * sum(1 for t in T if t['pnl'] > 0) / max(1, len(T))))
out2 = ['\nCOMBINATIONS of the loosenings that cost almost nothing alone (%d combinations). Sorted by total $; today: 36 trades +88.38 $' % len(res)]
for tot, n, name, kw, by, won in sorted(res, key=lambda x: -x[0])[:15]:
    out2.append('  %+8.2f $ | %3d trades (x%.1f) | won %3.0f%% | %+5.2f a trade | %s | %s' % (tot, n, n / 36.0, won, tot / n, ' / '.join('%+.1f' % x for x in by), name))
out2.append('\n  MOST TRADES while the total stays at +80 $ or more:')
for tot, n, name, kw, by, won in sorted([r for r in res if r[0] >= 80], key=lambda x: -x[1])[:8]:
    out2.append('  %3d trades (x%.1f) | %+8.2f $ | won %3.0f%% | %+5.2f a trade | %s | %s' % (n, n / 36.0, tot, won, tot / n, ' / '.join('%+.1f' % x for x in by), name))
everything = max(res, key=lambda r: r[1])
out2.append('\n  ALL SIX together: %d trades | %+.2f $ | won %.0f%% | %s' % (everything[1], everything[0], everything[5], ' / '.join('%+.1f' % x for x in everything[4])))
txt2 = '\n'.join(out2); print(txt2)
open(os.path.expanduser('~/swifteye/research/rule_adjust.txt'), 'a').write(txt2 + '\n')
