"""More trades OUTSIDE the prime window 13:00-22:00 Norway (owner, 22 Nov 2026: "how can we get more trades outside of the prime
window (13-22)?").

Extended replay (SWEEP_DATA=v18, strict gap checks, 17 Nov 05:00 - 22 Nov 01:00), the bot as updated on 22 Nov: 'followed' on
Polymarket's own clock + 5 s (tools/lag_shift.py), give up at 15 min, real-money sales, $12 flat.
  1 by hour (Norway): games with a candidate move, candidate moves, trades, won, $
  2 the rule funnel, prime (13-22) v off-prime: the share of moves surviving each rule
  3 off-prime: sports and trigger bookmakers of the candidates and the trades
  4 each rule loosened ONLY off-prime (prime keeps today's rules): trades and $ off-prime, by day, and a leave-one-day-out
    check over the loosenings (the one best on 4 days, scored on the 5th)
usage: SWEEP_DATA=v18 python3 offprime.py      -> ~/swifteye/research/offprime.txt
"""
import os, sys, io, contextlib, collections, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
S = LS.S
ROWS = LS.rows_with(5000)
B = dict(S.BASE, real_sale=True, give=900); CI = S.CATCH.index(1.0)
hour = lambda c: int(time.strftime('%H', time.localtime(c['tf'] / 1000)))
prime = lambda c: 13 <= hour(c) < 22
days = sorted({c['_day'] for c in ROWS})
out = []; P = out.append
T = S.sim(B, rows=ROWS)
clean = [c for c in ROWS if not c['blind']]
P('MORE TRADES OUTSIDE 13-22? The bot as updated 22 Nov (5 s slack, 15-min give-up), replay 17 Nov 05:00 - 22 Nov 01:00, $12 flat\n')
P('1 BY HOUR (Norway)   games  candidate moves  trades  won   $')
for h in range(24):
    G = {c['match_id'] for c in clean if hour(c) == h}; M = sum(1 for c in clean if hour(c) == h); X = [t for t in T if hour(t['c']) == h]
    P('  %02d-%02d %s %6d %10d %10d %5d %+8.2f' % (h, h + 1, '*' if 13 <= h < 22 else ' ', len(G), M, len(X), sum(1 for t in X if t['pnl'] > 0), sum(t['pnl'] for t in X)))
for lab, f in (('PRIME 13-22', prime), ('OFF-PRIME', lambda c: not prime(c))):
    X = [t for t in T if f(t['c'])]
    P('  %-12s trades %3d, won %3d (%3.0f%%), %+8.2f $ | candidate moves %d in %d games' % (lab, len(X), sum(1 for t in X if t['pnl'] > 0), 100.0 * sum(1 for t in X if t['pnl'] > 0) / max(1, len(X)), sum(t['pnl'] for t in X),
        sum(1 for c in clean if f(c)), len({c['match_id'] for c in clean if f(c)})))
# 2 funnel
steps = [('candidate moves (7 pt+)', lambda c: True), ('8 pt+', lambda c: c['_mv'] >= B['min_move']), ('not followed yet (5%)', lambda c: c['_fol'] <= B['max_followed']),
         ('2+ families + consensus 3 pt', lambda c: c['cx']['fam_moved'] >= B['fams'] and c['cx']['cons_move'] >= B['cons']), ('spread 4c-', lambda c: c['_sp'] <= B['max_spread'] + 1e-9),
         ('expected gain 4c+', lambda c: c['_emin'][CI] >= B['min_edge']), ('basketball/Dota/weak cap', lambda c: not (c['sport'] == 'basketball' and c['_usbb']) and not (c['sport'] == 'dota2' and c['_sp'] > 0.02 + 1e-9) and not (c['_px'] - 0.01 > 0.60 + 1e-9 and (c['cx']['jumps'] == 0 or c['cx']['follow'] - c['cx']['lead'] < 0.2))),
         ('no pause, 90c-', lambda c: not c['pause'] and c['_px'] <= B['cap']), ('lagged 50% (5 s slack)', lambda c: c['cx']['jumps'] > 0 and c['cx']['follow'] >= B['lag']),
         ('gap rule', lambda c: c['_gap'] is None or c['_gap'] < B['gap']), ('own clock: not followed', lambda c: c['_tfol'] is None or c['_tfol'] <= B['max_followed'])]
P('\n2 THE RULE FUNNEL: moves left after each rule, prime v off-prime (share of the step before)')
A = [c for c in clean if prime(c)]; Z = [c for c in clean if not prime(c)]
for name, f in steps:
    a2 = [c for c in A if f(c)]; z2 = [c for c in Z if f(c)]
    P('  %-30s prime %6d (%3.0f%%) | off-prime %6d (%3.0f%%)' % (name, len(a2), 100.0 * len(a2) / max(1, len(A)), len(z2), 100.0 * len(z2) / max(1, len(Z))))
    A, Z = a2, z2
# 3 sports / triggers off-prime
P('\n3 OFF-PRIME: candidate moves by sport %s' % dict(collections.Counter(c['sport'] for c in clean if not prime(c)).most_common(8)))
P('  trades by sport %s' % dict(collections.Counter(t['c']['sport'] for t in T if not prime(t['c'])).most_common()))
P('  candidate moves by trigger %s' % dict(collections.Counter(c['trigger'] for c in clean if not prime(c)).most_common(10)))
# 4 loosen only off-prime
LOOSE = [('move 7 pt', dict(min_move=0.07)), ('followed up to 10%', dict(max_followed=0.10)), ('followed up to 15%', dict(max_followed=0.15)), ('1 family enough', dict(fams=1)),
         ('consensus 2 pt', dict(cons=0.02)), ('consensus any', dict(cons=0.0)), ('spread up to 6c', dict(max_spread=0.06)), ('expected gain 2c', dict(min_edge=0.02)),
         ('expected gain 0c', dict(min_edge=0.0)), ('lagged 40%', dict(lag=0.4)), ('lagged 30%', dict(lag=0.3)), ('lagged off', dict(lag=None)), ('weak cap 70c', dict(weak_cap=0.70)),
         ('weak cap off', dict(weak_cap=None)), ('after a pause too', dict(pause=False)), ('no gap rule', dict(gap=None)), ('US basketball too', dict(bb='all')),
         ('give up at 30 min', dict(give=1800))]


def offp(kw):
    """Prime keeps today's rules; off-prime uses kw. Returns per-day off-prime $ and trades."""
    Tp = [t for t in S.sim(B, rows=ROWS) if prime(t['c'])]
    To = [t for t in S.sim(dict(B, **kw), rows=ROWS) if not prime(t['c'])]
    by = collections.defaultdict(float); n = collections.Counter()
    for t in To: by[t['c']['_day']] += t['pnl']; n[t['c']['_day']] += 1
    return [by[d] for d in days], [n[d] for d in days], sum(1 for t in To if t['pnl'] > 0), Tp
base = offp({})
P('\n4 LOOSEN ONLY OFF-PRIME (prime keeps today\'s rules; the two halves are run separately). Off-prime with today\'s rules: %d trades, %+.2f $ | by day %s' % (sum(base[1]), sum(base[0]), ' / '.join('%+.1f' % x for x in base[0])))
res = [("today's rules", {}, base[0], base[1], base[2])]
for name, kw in LOOSE:
    pd, nd, w, _ = offp(kw); res.append((name, kw, pd, nd, w))
    P('  %-22s off-prime %3d trades (%+3d), won %3d, %+8.2f $ (%+7.2f v now) | by day %s' % (name, sum(nd), sum(nd) - sum(base[1]), w, sum(pd), sum(pd) - sum(base[0]), ' / '.join('%+.1f' % x for x in pd)))
P('\n  LEAVE ONE DAY OUT over these off-prime choices:')
tot = 0.0
for i, d in enumerate(days):
    pick = max(res, key=lambda r: sum(r[2]) - r[2][i]); tot += pick[2][i]
    P('    %s Nov: picked "%s" -> %+.2f $ (%d trades) off-prime that day (today\'s rules %+.2f $)' % (d, pick[0], pick[2][i], pick[3][i], base[0][i]))
P('    SUM: %+.2f $ v today\'s rules off-prime %+.2f $' % (tot, sum(base[0])))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/offprime.txt'), 'w').write(txt + '\n')
