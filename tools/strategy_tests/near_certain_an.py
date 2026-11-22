"""Test 2, analysis: buy the near-certain side on Polymarket when the bookmakers price it higher. Hold to the result."""
from sx import *
R = json.load(open('t2.json'))
for r in R:
    f = r['fair']; r['nf'] = len(f); r['fm'] = st.median(f) if len(f) >= 2 else None; r['fmin'] = min(f) if len(f) >= 2 else None
U = [r for r in R if r['ask5'] is not None and 0.80 <= r['ask5'] <= 0.99 and r['age'] <= 15 and r['ask'] - r['bid'] <= 0.03]
roi = lambda x: st.mean((r['won'] - r['ask5'] - fee(r['ask5'])) / (r['ask5'] + fee(r['ask5'])) for r in x)
byg = lambda x: (lambda d: ([d[r['m']].append(r) for r in x], d)[1])(defaultdict(list))
print(f"rows {len(U)} (a side offered at 88-98.5c, checked every 3 min in play) from {len({r['m'] for r in U})} games; with 2+ bookmaker groups quoting: {sum(r['fm'] is not None for r in U)}")
print("sports:", Counter(r['sport'] for r in {r['m']: r for r in U}.values()).most_common(6))
def line(name, x):
    if len(x) < 12: print(f"  {name:44s} rows {len(x)}"); return
    lo, hi = boot(byg(x), lambda z: roi(z) if z else None)
    print(f"  {name:44s} rows {len(x):5d} games {len({r['m'] for r in x}):4d} | paid {st.mean(r['ask5'] for r in x)*100:5.1f}c | won {st.mean(r['won'] for r in x)*100:5.1f}% | return per $ {roi(x)*100:+5.1f}% (90% range {lo*100:+5.1f} .. {hi*100:+5.1f})")
print("\nA. BY POLYMARKET'S PRICE ALONE (every row bought)")
for lo_, hi_ in ((0.88, 0.91), (0.91, 0.94), (0.94, 0.96), (0.96, 0.99)):
    line(f"ask {lo_*100:.0f}-{hi_*100:.0f}c", [r for r in U if lo_ <= r['ask5'] < hi_])
print("\nB. BY WHAT THE BOOKMAKERS SAY ABOUT THAT SIDE AT THAT MOMENT (every row bought)")
line("no bookmaker quoting (suspended or closed)", [r for r in U if r['fm'] is None])
for lo_, hi_ in ((0, 0.85), (0.85, 0.92), (0.92, 0.95), (0.95, 0.97), (0.97, 0.985), (0.985, 2)):
    x = [r for r in U if r['fm'] is not None and lo_ <= r['fm'] < hi_]
    line(f"bookmakers say {lo_*100:.0f}-{min(hi_,1)*100:.0f}%", x)
print("\nC. BY THE GAP: bookmakers' fair price minus Polymarket's ask (rows with bookmakers quoting)")
for lo_, hi_ in ((-9, -0.03), (-0.03, -0.01), (-0.01, 0.01), (0.01, 0.02), (0.02, 0.04), (0.04, 9)):
    x = [r for r in U if r['fm'] is not None and lo_ <= r['fm'] - r['ask5'] < hi_]
    line(f"fair minus ask {lo_*100:+4.0f}..{hi_*100:+4.0f} pt" if abs(lo_) < 5 and abs(hi_) < 5 else (f"fair minus ask under {hi_*100:+.0f} pt" if lo_ < -5 else f"fair minus ask over {lo_*100:+.0f} pt"), x)
print("\nD. AS A STRATEGY: one bet a game, the first time the bookmakers say F or more and the edge after the fee is X or more")
def strat(F, X, key='fm', lo_ask=0.80):
    bets = {}
    for r in sorted(U, key=lambda r: r['t']):
        if r['m'] in bets or r[key] is None: continue
        if r[key] >= F and r[key] - r['ask5'] - fee(r['ask5']) >= X and r['ask5'] >= lo_ask: bets[r['m']] = r
    return list(bets.values())
def show(name, b):
    if len(b) < 10: print(f"  {name:50s} bets {len(b)}"); return
    lo, hi = boot({r['m']: [r] for r in b}, lambda z: roi(z) if z else None)
    cap = st.median((r['size5'] or 0) * r['ask5'] for r in b)
    print(f"  {name:50s} bets {len(b):4d} | paid {st.mean(r['ask5'] for r in b)*100:4.1f}c | won {st.mean(r['won'] for r in b)*100:5.1f}% (break-even {st.mean(r['ask5']+fee(r['ask5']) for r in b)*100:4.1f}%) | return per $ {roi(b)*100:+5.1f}% (90% range {lo*100:+5.1f} .. {hi*100:+5.1f}) | lost {sum(1-r['won'] for r in b)} | $ at the best price, median {cap:,.0f}")
for F in (0.92, 0.95, 0.97):
    for X in (0.0, 0.01, 0.02):
        show(f"bookmakers {F*100:.0f}%+, edge {X*100:.0f}+ pt", strat(F, X))
show("EVERY group says 95%+, edge 1+ pt", strat(0.95, 0.01, key='fmin'))
show("EVERY group says 97%+, edge 0+ pt", strat(0.97, 0.0, key='fmin'))
base = {}
for r in sorted(U, key=lambda r: r['t']):
    if r['m'] not in base and 0.93 <= r['ask5'] <= 0.97: base[r['m']] = r
show("comparison: first time a side costs 93-97c, bookmakers ignored", list(base.values()))
b = strat(0.95, 0.01)
print("\n  the losing bets of 'bookmakers 95%+, edge 1+ pt':")
for r in b:
    if not r['won']: print(f"    game {r['m']} {r['sport']:10s} {r['rel']:4d} min after the start | paid {r['ask5']*100:.0f}c | bookmakers {r['fm']*100:.1f}% ({r['nf']} groups, lowest {r['fmin']*100:.1f}%)")
print("  by sport:", {s: (len(x), f"{st.mean(r['won'] for r in x)*100:.0f}% won", f"{roi(x)*100:+.1f}%") for s, x in ((s, [r for r in b if r['sport'] == s]) for s in ('tennis', 'basketball', 'cs2', 'football', 'hockey')) if len(x) >= 5})
