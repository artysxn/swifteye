"""Test 1, analysis: after the bookmakers' pre-match price moves, does buying on Polymarket pay?"""
from sx import *
R = json.load(open('t1.json'))
U = [r for r in R if 0.05 <= r['ask'] <= 0.95 and r['ask'] - r['bid'] <= 0.06]
for r in U:
    r['fol'] = (r['pm1'] - r['pm0']) / r['move']; r['mid'] = (r['bid'] + r['ask']) / 2
    r['hold'] = (r['won'] - r['ask'] - fee(r['ask'])) / (r['ask'] + fee(r['ask']))
    s = r['later'].get('start'); r['sell'] = (s[0] - fee(s[0]) - r['ask'] - fee(r['ask'])) / (r['ask'] + fee(r['ask'])) if s else None
    r['gap'] = r['fb'] - r['ask']
byg = lambda x: (lambda d: ([d[r['m']].append(r) for r in x], d)[1])(defaultdict(list))
print(f"{len(U)} bookmaker moves of 1.5+ pt within 15 min, before the start, in {len({r['m'] for r in U})} games (3+ groups quoting, 2+ moved)")
print("sports:", Counter(r['sport'] for r in U).most_common(6), "| hours before the start, median", round(st.median(r['to_start'] for r in U), 1))
def drift(x, h):
    v = [(r['later'][h][0] + r['later'][h][1]) / 2 - r['mid'] for r in x if r['later'].get(h)]
    return f"{st.mean(v)*100:+5.2f}" if len(v) >= 8 else "    -"
def line(name, x):
    if len(x) < 12: print(f"  {name:36s} moves {len(x)}"); return
    lo, hi = boot(byg(x), lambda z: st.mean(r['hold'] for r in z) if z else None)
    sv = [r['sell'] for r in x if r['sell'] is not None]
    l2, h2 = boot(byg([r for r in x if r['sell'] is not None]), lambda z: st.mean(r['sell'] for r in z) if z else None)
    print(f"  {name:36s} moves {len(x):4d} games {len({r['m'] for r in x}):4d} | move {st.mean(r['move'] for r in x)*100:4.1f} pt, Polymarket had followed {st.median(r['fol'] for r in x)*100:4.0f}% | its price after 5/15/60 min/at start: {drift(x,'5')} {drift(x,'15')} {drift(x,'60')} {drift(x,'start')} pt"
          f" | sell before the start {st.mean(sv)*100:+5.1f}% ({l2*100:+4.0f}..{h2*100:+4.0f}) | hold to the result {st.mean(r['hold'] for r in x)*100:+5.1f}% ({lo*100:+4.0f}..{hi*100:+4.0f})")
print("\nA. BY THE SIZE OF THE BOOKMAKER MOVE (the side that rose, bought on Polymarket 30 s later)")
for lo_, hi_ in ((0.015, 0.025), (0.025, 0.04), (0.04, 0.07), (0.07, 9)):
    line(f"move {lo_*100:.1f}-{hi_*100:.0f} pt" if hi_ < 5 else f"move {lo_*100:.0f}+ pt", [r for r in U if lo_ <= r['move'] < hi_])
print("\nB. BY HOW MUCH OF THE MOVE POLYMARKET HAD ALREADY MADE (moves of 2.5+ pt)")
B = [r for r in U if r['move'] >= 0.025]
line("had not followed (under 20%)", [r for r in B if r['fol'] < 0.2])
line("partly (20-70%)", [r for r in B if 0.2 <= r['fol'] < 0.7])
line("already there (70%+)", [r for r in B if r['fol'] >= 0.7])
print("\nC. BY POLYMARKET'S PRICE AGAINST THE BOOKMAKERS' NEW FAIR PRICE (moves of 2.5+ pt)")
line("Polymarket ask 2+ pt under fair", [r for r in B if r['gap'] >= 0.02])
line("within 2 pt", [r for r in B if -0.02 <= r['gap'] < 0.02])
line("Polymarket ask 2+ pt over fair", [r for r in B if r['gap'] < -0.02])
print("\nD. BY TIME TO THE START (moves of 2.5+ pt, Polymarket followed under 20%)")
D = [r for r in B if r['fol'] < 0.2]
line("over 3 hours", [r for r in D if r['to_start'] > 3]); line("1-3 hours", [r for r in D if 1 < r['to_start'] <= 3]); line("under 1 hour", [r for r in D if r['to_start'] <= 1])
print("\nE. AS A STRATEGY: the first such move in each game")
for thr, f in ((0.025, 0.2), (0.025, 9), (0.04, 0.2), (0.04, 9)):
    first = {}
    for r in sorted(U, key=lambda r: r['t']):
        if r['m'] not in first and r['move'] >= thr and r['fol'] < f: first[r['m']] = r
    line(f"move {thr*100:.1f}+ pt" + (", followed under 20%" if f < 1 else ", any follow"), list(first.values()))
x = [r for r in B if r['fol'] < 0.2]
if len(x) >= 12: print(f"\n  who was right on those {len(x)}: bookmakers said {st.mean(r['fb'] for r in x)*100:.1f}%, Polymarket asked {st.mean(r['ask'] for r in x)*100:.1f}c, the side won {st.mean(r['won'] for r in x)*100:.1f}%")
