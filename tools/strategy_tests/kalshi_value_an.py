"""Test 4, analysis: is Kalshi softer than the bookmakers or Polymarket? Kalshi fee = 7% x p x (1-p)."""
from sx import *
kfee = lambda a: 0.07 * a * (1 - a)
R = json.load(open('t4.json'))
for r in R:
    r['mid'] = (r['bid'] + r['ask']) / 2; r['phase'] = 'pre' if r['rel'] < 0 else ('live' if r['live'] >= 15 else 'quiet')
    r['pm'] = (r['pm_bid'] + r['pm_ask']) / 2 if r['pm_bid'] is not None and r['pm_ask'] is not None else None
U = [r for r in R if 0.05 <= r['ask'] <= 0.95 and r['ask'] - r['bid'] <= 0.08 and r['ask30'] is not None and 0.03 <= r['ask30'] <= 0.97 and r['age'] <= 3600]
byg = lambda x: (lambda d: ([d[r['m']].append(r) for r in x], d)[1])(defaultdict(list))
brier = lambda x, k: st.mean((r[k] - r['won']) ** 2 for r in x)
roi = lambda x: st.mean((r['won'] - r['ask30'] - kfee(r['ask30'])) / (r['ask30'] + kfee(r['ask30'])) for r in x)
print(f"usable rows {len(U)} from {len({r['m'] for r in U})} settled Kalshi games; sports:", Counter(r['sport'] for r in {r['m']: r for r in U}.values()).most_common(6))
print(f"Kalshi spread median {st.median(r['ask']-r['bid'] for r in U)*100:.0f}c | fee at 50c {kfee(.5)*100:.2f}c")
print("\nA. WHO PREDICTS THE RESULT BETTER (squared error, lower = better)")
for name, x in (('all', U), ('pre-match', [r for r in U if r['phase'] == 'pre']), ('in play', [r for r in U if r['phase'] == 'live'])):
    lo, hi = boot(byg(x), lambda z: brier(z, 'mid') - brier(z, 'fair') if z else None)
    print(f"  {name:9s} rows {len(x):5d} games {len({r['m'] for r in x}):4d}: Kalshi {brier(x,'mid'):.4f} | bookmakers {brier(x,'fair'):.4f} | difference {brier(x,'mid')-brier(x,'fair'):+.4f} (90% range {lo:+.4f} .. {hi:+.4f}; above 0 = bookmakers better)")
P = [r for r in U if r['pm'] is not None]
if len(P) > 100:
    lo, hi = boot(byg(P), lambda z: brier(z, 'mid') - brier(z, 'pm') if z else None)
    print(f"  rows also on Polymarket {len(P)}, games {len({r['m'] for r in P})}: Kalshi {brier(P,'mid'):.4f} | Polymarket {brier(P,'pm'):.4f} | bookmakers {brier(P,'fair'):.4f} | Kalshi minus Polymarket {brier(P,'mid')-brier(P,'pm'):+.4f} ({lo:+.4f} .. {hi:+.4f})")
print("\nB. ONE BET A GAME ON KALSHI at the first check with enough edge after the fee, held to the result")
def strat(rows, thr, ref, phases=('pre', 'live', 'quiet'), cap=0.15, mode='edge'):
    bets = {}; by = defaultdict(list)
    for r in rows:
        if r['phase'] in phases: by[(r['t'], r['m'])].append(r)
    for (t, m) in sorted(by):
        if m in bets: continue
        cnd = []
        for r in by[(t, m)]:
            if mode == 'random': cnd.append((int(hashlib.md5(f"{m}{t}{r['o']}".encode()).hexdigest()[:8], 16), r)); continue
            if r[ref] is None: continue
            e = r[ref] - r['ask30'] - kfee(r['ask30'])
            if thr <= e <= cap: cnd.append((e, r))
        if cnd: bets[m] = max(cnd, key=lambda x: x[0])[1]
    return list(bets.values())
def show(name, b):
    if len(b) < 10: print(f"  {name:56s} bets {len(b)}"); return
    lo, hi = boot({r['m']: [r] for r in b}, lambda z: roi(z) if z else None)
    print(f"  {name:56s} bets {len(b):4d} | paid {st.mean(r['ask30'] for r in b)*100:4.1f}c | won {st.mean(r['won'] for r in b)*100:4.1f}% (break-even {st.mean(r['ask30']+kfee(r['ask30']) for r in b)*100:4.1f}%) | return per $ {roi(b)*100:+6.1f}% (90% range {lo*100:+4.0f} .. {hi*100:+4.0f})")
show("comparison: a random side of every game", strat(U, 0, None, mode='random'))
for thr in (0.0, 0.01, 0.02, 0.03): show(f"Kalshi ask under the bookmakers' fair by {thr*100:.0f}+ pt", strat(U, thr, 'fair'))
for thr in (0.01, 0.02):
    show(f"   pre-match only, {thr*100:.0f}+ pt", strat(U, thr, 'fair', phases=('pre',))); show(f"   in play only, {thr*100:.0f}+ pt", strat(U, thr, 'fair', phases=('live',)))
for thr in (0.0, 0.01, 0.02): show(f"Kalshi ask under Polymarket's mid by {thr*100:.0f}+ pt", strat(P, thr, 'pm'))
g = sorted(abs(r['mid'] - r['fair']) for r in U); g2 = sorted(abs(r['mid'] - r['pm']) for r in P) if P else [0]
print("\nsize of the gaps: Kalshi vs bookmakers " + ', '.join(f"{p}% under {g[int(len(g)*p/100)]*100:.1f} pt" for p in (50, 90)) + (" | Kalshi vs Polymarket " + ', '.join(f"{p}% under {g2[int(len(g2)*p/100)]*100:.1f} pt" for p in (50, 90)) if P else ''))
