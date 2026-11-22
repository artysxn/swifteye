"""Test 3, analysis: Polymarket drops 4+ points in 10 s. If the bookmakers did not move, does it come back?"""
from sx import *
R = json.load(open('t3.json'))
U = [r for r in R if r['e_ask'] is not None and 0.05 <= r['e_ask'] <= 0.95 and r['e_ask'] - r['e_bid'] <= 0.06]
for r in U:
    r['drop'] = r['m0'] - r['m1']; r['emid'] = (r['e_bid'] + r['e_ask']) / 2
    r['still'] = (r['m0'] - r['emid']) / r['drop']          # share of the drop still there 20 s later
    if r['n0'] < 3 or r['f0'] is None: r['cls'] = 'no bookmakers'
    elif r['n20'] < 3 or r['f20'] is None or r['susp'] > 0: r['cls'] = 'bookmakers paused'
    elif r['f20'] - r['f0'] <= -0.02: r['cls'] = 'bookmakers dropped too'
    elif abs(r['f20'] - r['f0']) < 0.01: r['cls'] = 'bookmakers did not move'
    else: r['cls'] = 'bookmakers moved a little'
    a = r['e_ask']; r['hold'] = (r['won'] - a - fee(a)) / (a + fee(a))
    for k in ('1', '3', '5', '15'):
        v = r['later'][k]; r['s' + k] = (v[0] - fee(v[0]) - a - fee(a)) / (a + fee(a)) if v else None; r['d' + k] = ((v[0] + v[1]) / 2 - r['emid']) if v else None
byg = lambda x: (lambda d: ([d[r['m']].append(r) for r in x], d)[1])(defaultdict(list))
print(f"{len(U)} drops of 4+ pt within 10 s, in play, in {len({r['m'] for r in U})} games (every 4th settled game); sports:", Counter(r['sport'] for r in U).most_common(5))
def line(name, x):
    if len(x) < 12: print(f"  {name:40s} drops {len(x)}"); return
    def col(k):
        v = [r[k] for r in x if r[k] is not None]
        if len(v) < 8: return "     -"
        lo, hi = boot(byg([r for r in x if r[k] is not None]), lambda z: st.mean(r[k] for r in z) if z else None, 300)
        return f"{st.mean(v)*100:+5.1f}% ({lo*100:+3.0f}..{hi*100:+3.0f})"
    mv = lambda k: (lambda v: f"{st.mean(v)*100:+4.1f}" if len(v) >= 8 else "   -")([r[k] for r in x if r[k] is not None])
    print(f"  {name:40s} drops {len(x):4d} games {len({r['m'] for r in x}):3d} | drop {st.mean(r['drop'] for r in x)*100:4.1f} pt, {st.median(r['still'] for r in x)*100:3.0f}% still there at the buy | price after 1/3/5/15 min: {mv('d1')} {mv('d3')} {mv('d5')} {mv('d15')} pt"
          f" | buy 20 s after, sell after 3 min {col('s3')} | after 15 min {col('s15')} | hold {col('hold')}")
print("\nA. BY WHAT THE BOOKMAKERS DID (from 10 s before the drop to 20 s after)")
for cl in ('bookmakers did not move', 'bookmakers moved a little', 'bookmakers dropped too', 'bookmakers paused', 'no bookmakers'):
    line(cl, [r for r in U if r['cls'] == cl])
P = [r for r in U if r['cls'] == 'bookmakers did not move']
print("\nB. POLYMARKET-ONLY DROPS, by what else is known at the buy")
line("drop still 70%+ there 20 s later", [r for r in P if r['still'] >= 0.7])
line("already half back", [r for r in P if r['still'] < 0.5])
line("drop 4-6 pt", [r for r in P if r['drop'] < 0.06]); line("drop 6+ pt", [r for r in P if r['drop'] >= 0.06])
line("Polymarket now 3+ pt under the bookmakers", [r for r in P if r['f20'] - r['e_ask'] >= 0.03])
line("Polymarket still at or over the bookmakers", [r for r in P if r['f20'] - r['e_ask'] < 0.0])
for s in ('tennis', 'basketball', 'cs2'): line(f"{s}", [r for r in P if r['sport'] == s])
print("\nC. DID THE BOOKMAKERS FOLLOW LATER? (Polymarket-only drops with bookmakers still quoting after 60 s)")
x = [r for r in P if r['f60'] is not None and r['n60'] >= 3]
if len(x) >= 12: print(f"  {len(x)} drops of {st.mean(r['drop'] for r in x)*100:.1f} pt: 60 s later the bookmakers had moved {st.mean(r['f60']-r['f0'] for r in x)*100:+.2f} pt; they went down 2+ pt on {sum(r['f60']-r['f0'] <= -0.02 for r in x)} of them")
