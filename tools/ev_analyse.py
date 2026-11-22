"""Positive-EV test, step 2 (corrected): is Polymarket cheaper than the bookmakers' fair price a winning bet?
Fixes over the first pass: (1) the bookmakers' margin is removed with the power method (the simple
proportional way leaves underdogs about 2 points too high); (2) nothing depends on the order of rows."""
import json, math, random, statistics as st, hashlib
from collections import defaultdict, Counter
import sys
R = json.load(open(sys.argv[1] if len(sys.argv) > 1 else 'ev_snaps.json'))
fee = lambda a: 0.05 * a * (1 - a)
def power(odds):
    pi = [1 / o for o in odds]
    if sum(pi) <= 1.0: return [p / sum(pi) for p in pi]
    lo, hi = 1.0, 4.0
    for _ in range(40):
        k = (lo + hi) / 2
        if sum(p ** k for p in pi) > 1: lo = k
        else: hi = k
    return [p ** k for p in pi]
grp = defaultdict(list)
for r in R: grp[(r['m'], r['t'])].append(r)
for (m, t), rows in grp.items():
    fams = set.intersection(*[set(r['raw']) for r in rows]) if len(rows) == rows[0]['k'] else set()
    pw = {f: dict(zip([r['o'] for r in rows], power([r['raw'][f] for r in rows]))) for f in fams}
    for r in rows:
        r['fp'] = st.median(pw[f][r['o']] for f in fams) if len(fams) >= 3 else None
        r['pinp'] = pw['pinnacle'][r['o']] if 'pinnacle' in fams else None
        r['nfp'] = len(fams)
        r['over'] = st.median(sum(1 / x['raw'][f] for x in rows) for f in fams) if fams else None
for r in R:
    f = r['fair']; r['nf'] = len(f); r['fm'] = st.median(f.values())
    r['mid'] = (r['bid'] + r['ask']) / 2; r['sp'] = r['ask'] - r['bid']
    r['phase'] = 'pre' if r['rel'] < 0 else ('live' if r['live'] >= 15 else 'quiet')
    r['h'] = int(hashlib.md5(f"{r['m']}{r['t']}{r['o']}".encode()).hexdigest()[:8], 16)
ok = lambda r: 0.05 <= r['ask'] <= 0.95 and r['sp'] <= 0.06 and r['ask30'] is not None and 0.03 <= r['ask30'] <= 0.97
U = [r for r in R if ok(r) and r['nf'] >= 3 and r['fp'] is not None]
print(f"usable rows {len(U)} from {len({r['m'] for r in U})} games, 17-20 Nov; sports:", Counter(r['sport'] for r in {r['m']: r for r in U}.values()).most_common())
print(f"bookmaker margin (median of the group medians): {st.median(r['over'] for r in U)*100-100:.1f}%  | Polymarket spread median {st.median(r['sp'] for r in U)*100:.0f}c")
random.seed(3)
def boot(games, fn, n=500):
    ks = list(games); vals = []
    for _ in range(n):
        v = fn([x for k in (random.choice(ks) for _ in ks) for x in games[k]])
        if v is not None: vals.append(v)
    vals.sort()
    return (vals[int(len(vals) * .05)], vals[int(len(vals) * .95)]) if vals else (float('nan'), float('nan'))
bygame = lambda rows: (lambda d: ([d[r['m']].append(r) for r in rows], d)[1])(defaultdict(list))
brier = lambda rows, key: st.mean((r[key] - r['won']) ** 2 for r in rows)

print("\nA. WHO PREDICTS THE RESULT BETTER (squared error, lower = better)")
for name, rows in (('all', U), ('pre-match', [r for r in U if r['phase'] == 'pre']), ('in play', [r for r in U if r['phase'] == 'live'])):
    lo, hi = boot(bygame(rows), lambda x: brier(x, 'mid') - brier(x, 'fp') if x else None)
    print(f"  {name:9s} rows {len(rows):5d} games {len({r['m'] for r in rows}):4d}: Polymarket {brier(rows,'mid'):.4f} | bookmakers {brier(rows,'fp'):.4f} (simple margin removal {brier(rows,'fm'):.4f}) | difference {brier(rows,'mid')-brier(rows,'fp'):+.4f}, 90% range {lo:+.4f} .. {hi:+.4f}")
P = [r for r in U if r['pinp']]
print(f"  rows with Pinnacle {len(P)}, games {len({r['m'] for r in P})}: Polymarket {brier(P,'mid'):.4f} | Pinnacle {brier(P,'pinp'):.4f} | all bookmakers {brier(P,'fp'):.4f}")
def fit(rows, ref='fp', it=12):
    cl = lambda p: min(max(p, 1e-3), 1 - 1e-3); lg = lambda p: math.log(cl(p) / (1 - cl(p)))
    X = [(1.0, lg(r['mid']), lg(r[ref]) - lg(r['mid'])) for r in rows]; y = [r['won'] for r in rows]; w = [0.0, 1.0, 0.0]
    for _ in range(it):
        g = [0.0] * 3; Hm = [[0.0] * 3 for _ in range(3)]
        for x, t in zip(X, y):
            p = 1 / (1 + math.exp(-(w[0] + w[1] * x[1] + w[2] * x[2]))); q = p * (1 - p)
            for i in range(3):
                g[i] += (t - p) * x[i]
                for j in range(3): Hm[i][j] += q * x[i] * x[j]
        A = [Hm[i] + [g[i]] for i in range(3)]
        for i in range(3):
            pv = A[i][i]
            for j in range(i, 4): A[i][j] /= pv
            for k in range(3):
                if k != i:
                    fc = A[k][i]
                    for j in range(i, 4): A[k][j] -= fc * A[i][j]
        w = [w[i] + A[i][3] for i in range(3)]
    return w
lo, hi = boot(bygame(U), lambda x: fit(x)[2] if len(x) > 300 else None, 120)
print(f"  weight the result gives the bookmakers once Polymarket's price is known: {fit(U)[2]:.2f} (90% range {lo:.2f} .. {hi:.2f}; 0 = they add nothing, 1 = they are right and Polymarket wrong) | pre-match {fit([r for r in U if r['phase']=='pre'])[2]:.2f} | in play {fit([r for r in U if r['phase']=='live'])[2]:.2f}")

print("\nB. EVERY ROW BOUGHT 30 s LATER AT THE ASK AND HELD TO THE RESULT, by how far Polymarket's ask is below the bookmakers' fair price")
def roi(rows, f=True): return st.mean((r['won'] - r['ask30'] - (fee(r['ask30']) if f else 0)) / (r['ask30'] + (fee(r['ask30']) if f else 0)) for r in rows)
edges = [(-9, -0.03), (-0.03, -0.01), (-0.01, 0.01), (0.01, 0.03), (0.03, 0.06), (0.06, 0.15)]
for ph in ('pre', 'live'):
    print(f"  {'pre-match' if ph == 'pre' else 'in play'}")
    for lo_, hi_ in edges:
        x = [r for r in U if r['phase'] == ph and lo_ <= r['fp'] - r['ask30'] < hi_]
        if len(x) < 15: continue
        l, h = boot(bygame(x), lambda z: roi(z) if z else None, 300)
        print(f"    fair minus ask {lo_*100:+5.0f}..{hi_*100:+4.0f} pt: rows {len(x):5d} games {len({r['m'] for r in x}):4d} | paid {st.mean(r['ask30'] for r in x)*100:5.1f}c | bookmakers said {st.mean(r['fp'] for r in x)*100:5.1f}% | won {st.mean(r['won'] for r in x)*100:5.1f}% | return per $ {roi(x)*100:+6.1f}% (90% range {l*100:+5.0f} .. {h*100:+5.0f})")

print("\nC. AS A STRATEGY: one bet a game at the first check with enough edge after the fee (gaps over 15 pt left out as bad links)")
def strat(rows, thr, ref='fp', phases=('pre', 'live', 'quiet'), cap=0.15, mode='edge'):
    bets = {}
    by = defaultdict(list)
    for r in rows:
        if r['phase'] in phases: by[(r['t'], r['m'])].append(r)
    for (t, m) in sorted(by):
        if m in bets: continue
        c = []
        for r in by[(t, m)]:
            if mode == 'random': c.append((r['h'], r))
            elif mode == 'naive':
                pay = 1 / (r['ask30'] + fee(r['ask30'])); raws = list(r['raw'].values())
                if sum(pay > o for o in raws) * 2 > len(raws): c.append((r['h'], r))
            elif r[ref] is not None:
                e = r[ref] - r['ask30'] - fee(r['ask30'])
                if thr <= e <= cap: c.append((e, r))
        if c: bets[m] = max(c, key=lambda x: x[0])[1]
    return list(bets.values())
def show(name, bets):
    if len(bets) < 10: print(f"  {name:52s} bets {len(bets)}"); return
    lo, hi = boot({r['m']: [r] for r in bets}, lambda x: roi(x) if x else None)
    print(f"  {name:52s} bets {len(bets):4d} | paid {st.mean(r['ask30'] for r in bets)*100:4.1f}c | won {st.mean(r['won'] for r in bets)*100:4.1f}% (break-even {st.mean(r['ask30']+fee(r['ask30']) for r in bets)*100:4.1f}%) | return per $ {roi(bets)*100:+6.1f}% (90% range {lo*100:+5.0f} .. {hi*100:+5.0f})")
show("comparison: a random side of every game", strat(U, 0, mode='random'))
for thr in (0.0, 0.01, 0.02, 0.03):
    show(f"all bookmakers, edge >= {thr*100:.0f} pt, any time", strat(U, thr))
for thr in (0.0, 0.01, 0.02):
    show(f"   pre-match only, edge >= {thr*100:.0f} pt", strat(U, thr, phases=('pre',)))
for thr in (0.0, 0.02):
    show(f"   in play only, edge >= {thr*100:.0f} pt", strat(U, thr, phases=('live',)))
for thr in (0.0, 0.02):
    show(f"Pinnacle alone, edge >= {thr*100:.0f} pt", strat([r for r in U if r['pinp']], thr, ref='pinp'))
show("simple margin removal, edge >= 2 pt (first pass)", strat(U, 0.02, ref='fm'))
for sp in ('tennis', 'basketball', 'cs2'):
    show(f"   {sp}, edge >= 1 pt, pre-match", strat([r for r in U if r['sport'] == sp], 0.01, phases=('pre',)))

print("\nD. YOUR WORDING: Polymarket's payout after its fee beats the raw odds at most bookmaker groups")
nv = [r for r in U if sum((1 / (r['ask30'] + fee(r['ask30']))) > o for o in r['raw'].values()) * 2 > len(r['raw'])]
both = sum(1 for v in Counter((r['m'], r['t']) for r in nv).values() if v >= 2)
print(f"  true on {len(nv)} of {len(U)} rows ({len(nv)/len(U)*100:.0f}%); on {both} of {len(grp)} checks it is true for BOTH sides of the game at once")
show("one bet a game on that rule (side picked blindly)", strat(U, 0, mode='naive'))

print("\nE. WHO MOVES TOWARD WHOM between two pre-match checks of the same game (gap = bookmakers' fair - Polymarket mid, up to 15 pt)")
pre = defaultdict(list)
for r in U:
    if r['phase'] == 'pre': pre[(r['m'], r['o'])].append(r)
a = b = d = 0.0; n = 0
for v in pre.values():
    v.sort(key=lambda r: r['t'])
    for x, y in zip(v, v[1:]):
        g = x['fp'] - x['mid']
        if abs(g) > 0.15 or abs(g) < 0.01: continue
        a += g * (y['mid'] - x['mid']); b += g * (x['fp'] - y['fp']); d += g * g; n += 1
print(f"  pairs {n}: by the next check Polymarket has closed {a/d*100:.0f}% of the gap, the bookmakers {b/d*100:.0f}%")
g = sorted(abs(r['fp'] - r['mid']) for r in U)
print("\nsize of the gaps: " + ', '.join(f"{p}% of rows under {g[int(len(g)*p/100)]*100:.1f} pt" for p in (50, 75, 90, 97)))
