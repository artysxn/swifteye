"""Does the game's wave profile (who starts first, measured from paired legs in the hour BEFORE the move) pick
winners in the replay of 17-20 Nov? Same engine as sweep.py (rules, locks, fills, sales); the profile is only a
filter on which moves are taken. Polymarket's legs are timed by Polymarket's own clock.

usage: wave_profile_replay.py build   -> ~/swifteye/research/wave_profile.json   (a few minutes)
       wave_profile_replay.py test
"""
import sys, os, json, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
mode = sys.argv[1] if len(sys.argv) > 1 else 'test'
sys.argv = ['sweep.py', 'none']
import sweep as S, waves as W
OUT = os.path.expanduser('~/swifteye/research/wave_profile.json')
ci = S.CATCH.index(1.0)
LOOSE = dict(S.BASE, lag=None, weak_cap=None, min_move=0.07, max_followed=0.10, min_edge=0.03, dota_spread=None, bb='all', pause=False)
key = lambda c: '%d|%s|%d' % (c['match_id'], c['outcome'], c['t0'])
if mode == 'build':
    db = W.conn(); cand = [c for c in S.ROWS if S.gate(c, LOOSE, ci)]
    print('moves to profile:', len(cand), flush=True)
    prof = json.load(open(OUT)) if os.path.exists(OUT) else {}
    cache = {}; t_start = time.time()
    for n, c in enumerate(cand):
        k = key(c)
        if k in prof: continue
        ck = (c['match_id'], c['outcome'], c['dir'], c['t0'] // 30000)
        if ck not in cache:
            t1 = c['t0'] - 15000
            g = W.grid(db, c['match_id'], c['outcome'], float(c['dir']), t1 - 3600000, t1, clock='server')
            pr = W.pair(W.legs(g['cons'], W.TH_BK), W.legs(g['mid'], W.TH_PM)); m = [(B, P) for B, P in pr if P]
            cache[ck] = dict(n=len(pr), m=len(m), first=(sum(1 for B, P in m if P['b0'] < B['b0']) / len(m)) if m else None,
                             lag=st.median([P['b0'] - B['b0'] for B, P in m]) if m else None, ratio=st.median([abs(P['size'] / B['size']) for B, P in m]) if m else None,
                             endlag=st.median([P['i1'] - B['i1'] for B, P in m]) if m else None)
        prof[k] = cache[ck]
        if n % 100 == 0: print(n, 'of', len(cand), '%.0f s' % (time.time() - t_start), flush=True); json.dump(prof, open(OUT, 'w'))
    json.dump(prof, open(OUT, 'w')); print('written', OUT, len(prof))
else:
    prof = json.load(open(OUT))
    for c in S.ROWS: c['_wp'] = prof.get(key(c))
    def tot(T): return sum(t['pnl'] for t in T)
    def show(name, T, T0=None):
        days = collections.Counter(time.strftime('%d', time.localtime(t['c']['t0'] / 1000)) for t in T)
        dp = collections.defaultdict(float)
        for t in T: dp[time.strftime('%d', time.localtime(t['c']['t0'] / 1000))] += t['pnl']
        print('  %-64s trades %3d | won %3d (%2.0f%%) | $%+8.2f%s | by day %s' % (name, len(T), sum(1 for t in T if t['pnl'] > 0), 100.0 * sum(1 for t in T if t['pnl'] > 0) / max(len(T), 1), tot(T), (' (%+.2f)' % (tot(T) - tot(T0))) if T0 is not None else '', ' '.join('%s:%+.1f/%d' % (d, dp[d], days[d]) for d in sorted(days))))
    def flt(pred): return [c for c in S.ROWS if c['_wp'] is not None and c['_wp']['m'] >= 4 and pred(c['_wp'])]
    for title, P in (("TODAY'S RULES (incl. the bot's own 'lagged 50%' rule)", dict(S.BASE)), ("TODAY'S RULES WITHOUT the bot's 'lagged' rule", dict(S.BASE, lag=None, weak_cap=None)), ('LOOSER SET (7pt+, followed 10%, room 3c, every sport, no lagged rule)', LOOSE)):
        print('\n== ' + title)
        T0 = S.sim(P); show('all moves', T0)
        known = [c for c in S.ROWS if c['_wp'] is not None and c['_wp']['m'] >= 4]
        show('moves in games with 4+ paired legs in the last hour', S.sim(P, known))
        show('Polymarket starts its legs 5 s+ AFTER the bookmakers', S.sim(P, flt(lambda w: w['lag'] >= 5)))
        show('... 0 to 5 s after', S.sim(P, flt(lambda w: 0 <= w['lag'] < 5)))
        show('... BEFORE the bookmakers (leads)', S.sim(P, flt(lambda w: w['lag'] < 0)))
        show('... leads by 3 s+', S.sim(P, flt(lambda w: w['lag'] <= -3)))
        show('Polymarket first in under 40% of legs', S.sim(P, flt(lambda w: w['first'] < 0.4)))
        show('Polymarket first in 40-60%', S.sim(P, flt(lambda w: 0.4 <= w['first'] < 0.6)))
        show('Polymarket first in 60%+', S.sim(P, flt(lambda w: w['first'] >= 0.6)))
        show('RULE: skip games where Polymarket leads by 3 s+', S.sim(P, [c for c in S.ROWS if not (c['_wp'] is not None and c['_wp']['m'] >= 4 and c['_wp']['lag'] <= -3)]), T0)
        show('RULE: skip games where Polymarket is first in 60%+ of legs', S.sim(P, [c for c in S.ROWS if not (c['_wp'] is not None and c['_wp']['m'] >= 4 and c['_wp']['first'] >= 0.6)]), T0)
        show('RULE: only games where Polymarket starts 0 s+ after', S.sim(P, flt(lambda w: w['lag'] >= 0)), T0)
