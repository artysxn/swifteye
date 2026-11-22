"""Why is 18 Nov such an outlier, and how unlikely is it? (owner, 22 Nov 2026: "Why is 18 nov such an outlier? Standard
deviation says it's how likely to happen?")

Extended replay (SWEEP_DATA=v18, strict gap checks), today's rules, real-money sales, $12.
  per day   candidate moves and how many survive each rule; games with a Polymarket book in play that day; trades,
            win rate, $ a trade and its spread; sports and trigger bookmakers of the trades; how late the bot's own
            Polymarket feed was at those trades (feed_lag.json)
  odds      (1) the day's total against the other days (z, with and without 18 Nov in the mean); (2) the NUMBER of
            trades against the other days' rate (Poisson); (3) the $ a trade against the other days' trades (t-test and
            a shuffle test: 18 Nov's trades drawn at random from all 50, how often they make this much)
usage: SWEEP_DATA=v18 python3 day_outlier.py      -> ~/swifteye/research/day_outlier.txt
"""
import os, sys, json, math, random, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

B = dict(S.BASE, real_sale=True); CI = S.CATCH.index(1.0)
T = S.sim(B)
days = sorted({c['_day'] for c in S.ROWS})
out = []; P = out.append
L = json.load(open(os.path.expanduser('~/swifteye/research/feed_lag.json')))
P("WHY 18 NOV? today's rules on the extended replay (strict gap checks), real-money sales, $12\n")
steps = [('candidate moves (7 pt+)', lambda c: True), ('8 pt+', lambda c: c['_mv'] >= B['min_move']), ('not followed yet', lambda c: c['_fol'] <= B['max_followed']),
         ('2+ families, consensus', lambda c: c['cx']['fam_moved'] >= B['fams'] and c['cx']['cons_move'] >= B['cons']), ('spread 4c-', lambda c: c['_sp'] <= B['max_spread'] + 1e-9),
         ('gain 4c+', lambda c: c['_emin'][CI] >= B['min_edge']), ('lagged 50%', lambda c: c['cx']['jumps'] > 0 and c['cx']['follow'] >= B['lag'])]
rows = [c for c in S.ROWS if not c['blind']]
P('  %-28s ' % 'per day' + ' '.join('%9s' % ('%s Nov' % d) for d in days))
X = rows
for name, f in steps:
    X = [c for c in X if f(c)]
    P('  %-28s ' % name + ' '.join('%9d' % sum(1 for c in X if c['_day'] == d) for d in days))
P('  %-28s ' % 'games with candidate moves' + ' '.join('%9d' % len({c['match_id'] for c in rows if c['_day'] == d}) for d in days))
by = collections.defaultdict(list)
for t in T: by[t['c']['_day']].append(t)
P('  %-28s ' % 'TRADES' + ' '.join('%9d' % len(by[d]) for d in days))
P('  %-28s ' % 'won' + ' '.join('%8.0f%%' % (100.0 * sum(1 for t in by[d] if t['pnl'] > 0) / max(1, len(by[d]))) for d in days))
P('  %-28s ' % '$ total' + ' '.join('%+9.2f' % sum(t['pnl'] for t in by[d]) for d in days))
P('  %-28s ' % '$ a trade' + ' '.join('%+9.2f' % (sum(t['pnl'] for t in by[d]) / max(1, len(by[d]))) for d in days))
P('  %-28s ' % 'feed late at the trade (ms)' + ' '.join('%9s' % (('%.0f' % st.median([L[k] for k in (f"{t['c']['match_id']}|{t['c']['outcome']}|{t['c']['t0']}" for t in by[d]) if L.get(k) is not None])) if any(L.get(f"{t['c']['match_id']}|{t['c']['outcome']}|{t['c']['t0']}") is not None for t in by[d]) else '-') for d in days))
P('')
for d in days:
    if not by[d]: continue
    P('  %s Nov: sports %s | triggers %s' % (d, dict(collections.Counter(t['c']['sport'] for t in by[d]).most_common()), dict(collections.Counter(t['c']['trigger'] for t in by[d]).most_common(5))))
# ---- the odds
tot = {d: sum(t['pnl'] for t in by[d]) for d in days}; vals = [tot[d] for d in days]
P('\nHOW UNLIKELY IS 18 NOV?')
m, s = st.mean(vals), st.stdev(vals)
P('  (1) the day totals %s: mean %+.2f, standard deviation %.2f -> 18 Nov is %.1f SD above the mean' % (' / '.join('%+.2f' % v for v in vals), m, s, (tot['18'] - m) / s))
oth = [tot[d] for d in days if d != '18']; mo, so = st.mean(oth), st.stdev(oth)
tq = (tot['18'] - mo) / (so * math.sqrt(1 + 1 / len(oth)))
P('      against the OTHER days only (mean %+.2f, SD %.2f): %.1f SD; as a new day from that spread (t, %d degrees of freedom): t = %.2f' % (mo, so, (tot['18'] - mo) / so, len(oth) - 1, tq))
# Poisson for the count
lam = st.mean([len(by[d]) for d in days if d != '18']); k = len(by['18'])
p_ge = 1 - sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))
P('  (2) the NUMBER of trades: the other days average %.2f a day; %d or more in one day by chance (Poisson): p = %.5f (about 1 day in %.0f)' % (lam, k, p_ge, 1 / max(p_ge, 1e-12)))
# per-trade
a = [t['pnl'] for t in by['18']]; b = [t['pnl'] for d in days if d != '18' for t in by[d]]
se = math.sqrt(st.variance(a) / len(a) + st.variance(b) / len(b)); tt = (st.mean(a) - st.mean(b)) / se
P('  (3) $ a TRADE: 18 Nov %+.2f (SD %.2f, %d trades) v the other days %+.2f (SD %.2f, %d trades): t = %.2f' % (st.mean(a), st.stdev(a), len(a), st.mean(b), st.stdev(b), len(b), tt))
allp = a + b; random.seed(4); n18 = len(a); hits = 0; R = 20000
for _ in range(R):
    if sum(random.sample(allp, n18)) >= sum(a) - 1e-9: hits += 1
P('      shuffle test: %d of the 50 trades drawn at random make %+.2f $ or more in %.1f%% of %d draws' % (n18, sum(a), 100.0 * hits / R, R))
P('      if 18 Nov had the other days\' $ a trade (%+.2f) on its %d trades: %+.2f $ instead of %+.2f $' % (st.mean(b), n18, n18 * st.mean(b), sum(a)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/day_outlier.txt'), 'w').write(txt + '\n')
