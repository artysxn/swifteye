"""Patterns, step 2: is there ANY pocket where buying after a bookmaker jump pays, and what marks it?
The conditions of lf_patterns.py are added up into one score per event (each condition's effect measured on 17-19 Nov
only), and the events of 20-21 Nov, which the score has never seen, are then ranked by it.
usage: lf_pockets.py [60|120|300]   (holding time in seconds; default 120)"""
import os, sys, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_patterns as LP
H = int(sys.argv[1]) if len(sys.argv) > 1 else 120
C = LP.load(); E = [LP.measure(e) for e in LP.events(C)]
E = [e for e in E if e.get('best') is not None and e['spread'] <= 0.06 + 1e-9 and e['rt'][H] is not None]
def s_(e): return e['sb'] if e['sb'][0] >= 3 else e['sf']
def cut(v, edges): 
    if v is None: return 'none'
    for i, x in enumerate(edges):
        if v < x: return i
    return len(edges)
F = collections.OrderedDict([
    ('size of the jump', lambda e: cut(100 * e['size'], [6, 8, 12, 20])),
    ('bookmaker groups jumping', lambda e: min(len(e['fams']), 4)),
    ('Polymarket in the minute before', lambda e: cut(e['pre'], [-0.25, -0.05, 0.05, 0.25])),
    ('Polymarket in the 10 s before', lambda e: cut(e['pre10'], [-0.05, 0.05, 0.2])),
    ('delay score', lambda e: cut(s_(e)[4] if s_(e)[1] >= 2 else None, [-20, -3, 3.01, 20])),
    ('first share', lambda e: cut(s_(e)[2] / s_(e)[1] if s_(e)[1] >= 2 else None, [0.2, 0.4, 0.6])),
    ('follow', lambda e: cut(s_(e)[6] if s_(e)[1] >= 2 else None, [0.4, 0.6, 0.8])),
    ('response', lambda e: cut(s_(e)[5] if s_(e)[1] >= 2 else None, [0.3, 0.6, 0.9])),
    ('correlation score', lambda e: cut(e['corr'], [50, 80, 92, 97])),
    ('earlier jumps in the game', lambda e: cut(e['sb'][0], [1, 3, 8, 20, 50])),
    ('sport', lambda e: e['sport'] if e['sport'] in ('tennis', 'basketball', 'cs2', 'dota2', 'football', 'amfootball', 'valorant') else 'other'),
    ('bookmaker group', lambda e: LP.FAM.get(e['book'], e['book'])),
    ('price', lambda e: cut(100 * e['ask_e'], [20, 40, 60, 80])),
    ('spread', lambda e: cut(round(100 * e['spread']), [2, 3, 5])),
    ('after a pause', lambda e: cut(e['pause'], [1, 60])),
])
TR = [e for e in E if e['t'] < LP.SPLIT]; TE = [e for e in E if e['t'] >= LP.SPLIT]
y = lambda e: e['rt'][H]
mu = st.mean(y(e) for e in TR)
eff = {}
for name, f in F.items():
    g = collections.defaultdict(list)
    for e in TR: g[f(e)].append(y(e))
    eff[name] = {k: (st.mean(v) - mu) * len(v) / (len(v) + 300.0) for k, v in g.items()}
def score(e): return mu + sum(eff[n].get(f(e), 0.0) for n, f in F.items())
print('events: %d on 17-19 Nov (score built here), %d on 20-21 Nov (never seen by the score) | a buy and a sale %d s later, $12, spread and both fees' % (len(TR), len(TE), H))
print('average round trip: 17-19 Nov $%+.2f, 20-21 Nov $%+.2f' % (mu, st.mean(y(e) for e in TE)))
print('\nwhich conditions move the result most (best and worst bucket of each, $ a trade against the average, 17-19 Nov):')
for name in sorted(eff, key=lambda n: -(max(eff[n].values()) - min(eff[n].values()))):
    print('   %-34s spread of $%.2f' % (name, max(eff[name].values()) - min(eff[name].values())))
for lab, S in (('17-19 Nov (the days the score was built on)', TR), ('20-21 Nov (NEW days)', TE)):
    S2 = sorted(S, key=score, reverse=True); n = len(S2)
    print('\n' + lab + ': events ranked by the score, best first')
    for a, b, nm in ((0, 0.002, 'best 0.2%'), (0, 0.005, 'best 0.5%'), (0, 0.01, 'best 1%'), (0, 0.02, 'best 2%'), (0, 0.05, 'best 5%'), (0.05, 0.1, 'next 5%'), (0.1, 0.25, '10-25%'), (0.25, 0.5, '25-50%'), (0.5, 1.0, 'worst half')):
        g = S2[int(a * n):max(int(b * n), int(a * n) + 1)]
        days = collections.defaultdict(list)
        for e in g: days[time.strftime('%d', time.localtime(e['t'] / 1000))].append(y(e))
        print('   %-10s %5d events | round trip $%+5.2f a trade | won %3.0f%% | total $%+8.2f | score said $%+5.2f | by day %s' % (nm, len(g), st.mean(y(e) for e in g), 100.0 * sum(1 for e in g if y(e) > 0) / len(g), sum(y(e) for e in g), st.mean(score(e) for e in g), ' '.join('%s:%+.2f/%d' % (d, st.mean(v), len(v)) for d, v in sorted(days.items()))))
# what the best 1% of the new days look like
S2 = sorted(TE, key=score, reverse=True); top = S2[:max(int(0.01 * len(S2)), 1)]
print('\nwhat the best 1% on the new days are:')
for name, f in (('sport', lambda e: e['sport']), ('price band', lambda e: '%d-%dc' % (int(100 * e['ask_e']) // 20 * 20, int(100 * e['ask_e']) // 20 * 20 + 20)), ('groups jumping', lambda e: min(len(e['fams']), 4)), ('spread c', lambda e: round(100 * e['spread'])), ('jump pts', lambda e: int(100 * e['size']) // 4 * 4)):
    print('   %-14s %s' % (name, dict(collections.Counter(f(e) for e in top).most_common(6))))
