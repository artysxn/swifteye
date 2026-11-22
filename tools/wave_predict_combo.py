"""Follow-up to tools/wave_predict.py: the one signal that points the right way (bookmakers ahead of Polymarket by 6+
points over 60 s), alone and with each other signal, signed the bookmakers' way. How often a 5c+ wave follows THAT way
within 60 s, the other way, and the middle move after 30 s and 60 s (cents, + = the bookmakers' way).
usage: wave_predict_combo.py      -> ~/swifteye/research/paths/wave_predict_combo.txt"""
import os, sys, pickle, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave_predict as WP
isn = WP.isn
G = pickle.load(open(WP.OUT, 'rb'))
rows = []
for g in G:
    up, dn, _, _, _ = WP.labels(g)
    m = g['mid']; sp = [x for x in g['spread'] if not isn(x)]; usual = st.median(sp) if sp else 2.0
    ac = [x for x in g['act'] if not isn(x)]; uact = max(1.0, st.median(ac)) if ac else 1.0
    n = len(m)
    for i in range(30, n - 30):
        gp = g['gap60'][i]
        if isn(m[i]) or isn(up[i]) or isn(gp) or abs(gp) < 3: continue
        d = 1 if gp > 0 else -1
        f30 = (m[i + 15] - m[i]) * d if not isn(m[i + 15]) else None; f60 = (m[i + 30] - m[i]) * d if not isn(m[i + 30]) else None
        way, other = (up[i], dn[i]) if d > 0 else (dn[i], up[i])
        rows.append(dict(sport=g['sport'], gap=abs(gp), bkn=g['bkn'][i] * d, wide=g['spread'][i] > 1.5 * usual + 0.5, tight=g['spread'][i] <= 1.5, act=g['act'][i] / uact,
                         still=(not isn(m[i - 5]) and abs(m[i] - m[i - 5]) < 1), imb=g['imb'][i] * d, way=way, other=other, f30=f30, f60=f60))
out = []
def line(lab, X):
    if len(X) < 200: out.append('  %-58s %7d moments (too few)' % (lab, len(X))); return
    w = sum(1 for r in X if r['way'] >= 5 and r['way'] > r['other']) / len(X); o = sum(1 for r in X if r['other'] >= 5 and r['other'] > r['way']) / len(X)
    f30 = [r['f30'] for r in X if r['f30'] is not None]; f60 = [r['f60'] for r in X if r['f60'] is not None]
    out.append('  %-58s %7d moments | 5c+ the bookmakers\' way next %5.1f%% | the other way %5.1f%% | middle moved their way: 30 s %+5.2fc, 60 s %+5.2fc' % (lab, len(X), 100 * w, 100 * o, st.mean(f30), st.mean(f60)))
out.append('BOOKMAKERS AHEAD OF POLYMARKET (60 s), signed their way; %d moments of 400 recorded games' % len(rows))
A3 = [r for r in rows if 3 <= r['gap'] < 6]; A6 = [r for r in rows if r['gap'] >= 6]; A10 = [r for r in rows if r['gap'] >= 10]
line('ahead 3-6 pt', A3); line('ahead 6+ pt', A6); line('ahead 10+ pt', A10)
out.append('\n ahead 6+ pt, and ...')
for lab, fn in (('Polymarket has not moved in the last 10 s (under 1c)', lambda r: r['still']), ('Polymarket HAS moved in the last 10 s', lambda r: not r['still']),
                ('3+ bookmakers moved 3 pt their way in 30 s', lambda r: r['bkn'] >= 3), ('only 0-1 bookmakers moved 3 pt their way', lambda r: r['bkn'] <= 1),
                ('spread wide', lambda r: r['wide']), ('spread 1c', lambda r: r['tight']),
                ('book activity 3x+ the usual', lambda r: r['act'] >= 3), ('book quiet (under the usual)', lambda r: r['act'] < 1),
                ('order book leans their way (0.4+)', lambda r: r['imb'] >= 0.4), ('order book leans against (-0.4 or less)', lambda r: r['imb'] <= -0.4),
                ('Polymarket still + 3+ bookmakers + spread 1c', lambda r: r['still'] and r['bkn'] >= 3 and r['tight']),
                ('Polymarket still + 3+ bookmakers + activity 3x+', lambda r: r['still'] and r['bkn'] >= 3 and r['act'] >= 3)):
    line(lab, [r for r in A6 if fn(r)])
out.append('\n ahead 6+ pt, by sport')
for s in sorted({r['sport'] for r in A6}): line(s, [r for r in A6 if r['sport'] == s])
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/paths/wave_predict_combo.txt'), 'w').write(txt + '\n')
