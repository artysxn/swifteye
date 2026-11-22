"""Kalshi speed test, analysis: after Polymarket jumps, how long is Kalshi's old price still there, and does
buying it pay? Kalshi fee 7% x p x (1-p) per side. All times by the exchanges' own clocks unless said."""
from sx import *
kfee = lambda a: 0.07 * a * (1 - a)
R = json.load(open('k1.json'))
def at(path, dt, clock=0):
    best = None
    for p in path:
        if p[clock] <= dt and p[2] is not None and p[3] is not None:
            if best is None or p[clock] >= best[clock]: best = p
    return best
E = []
for r in R:
    k0 = at(r['path'], -10_000)
    if not k0 or k0[3] - k0[2] > 0.06: continue
    r['k0'] = (k0[2] + k0[3]) / 2; r['move'] = r['m1'] - r['m0']
    if abs(r['k0'] - r['m0']) > 0.05: continue            # the two exchanges agreed before the jump
    r['half'] = next((p[0] for p in r['path'] if p[0] > -10_000 and p[2] is not None and p[3] is not None and (p[2] + p[3]) / 2 - r['k0'] >= 0.5 * r['move']), None)
    E.append(r)
days = (max(r['t'] for r in E) - min(r['t'] for r in E)) / 86400e3
print(f"{len(E)} Polymarket jumps of 4+ pt within 10 s, in play, in {len({r['m'] for r in E})} games listed on both, over {days:.1f} days (Kalshi within 5 pt of Polymarket before the jump)")
print("sports:", Counter(r['sport'] for r in E).most_common(6), f"| average jump {st.mean(r['move'] for r in E)*100:.1f} pt")
print("\nA. HOW FAST KALSHI FOLLOWS")
h = sorted(r['half'] for r in E if r['half'] is not None)
print(f"  Kalshi reached half the jump within 5 min on {len(h)} of {len(E)}; time to get there: 25% of them within {h[len(h)//4]/1000:.1f} s, half within {h[len(h)//2]/1000:.1f} s, 75% within {h[len(h)*3//4]/1000:.1f} s  (negative = Kalshi moved first)")
print(f"  Kalshi moved BEFORE Polymarket finished its jump on {sum(x <= 0 for x in h)} of {len(h)} ({sum(x <= 0 for x in h)/len(h)*100:.0f}%)")
for L in (300, 1000, 2000, 4000, 10000):
    v = []
    for r in E:
        k = at(r['path'], L)
        if k: v.append(((k[2] + k[3]) / 2 - r['k0']) / r['move'])
    v.sort()
    print(f"  {L/1000:4.1f} s after the jump: Kalshi has made {st.median(v)*100:4.0f}% of it (median); still under 20% on {sum(x < 0.2 for x in v)/len(v)*100:3.0f}% of jumps")
v = []
for r in E:
    k = at(sorted(r['path'], key=lambda p: p[1]), 1000, clock=1)
    if k: v.append(((k[2] + k[3]) / 2 - r['k0']) / r['move'])
print(f"  for comparison, as OUR feed shows it 1 s after: {st.median(v)*100:.0f}% (median); still under 20% on {sum(x < 0.2 for x in v)/len(v)*100:.0f}%  -> the app's Kalshi feed is late, not Kalshi")

print("\nB. BUY THE OLD KALSHI PRICE (rules as the Polymarket snipe: Kalshi followed 20% or less, 3c+ of room after the fee, spread 4c or less, price 90c or less; one trade a game per 2 min)")
def sim(L, thr=0.03, maxfol=0.2):
    T = []; lock = {}
    for r in sorted(E, key=lambda r: r['t']):
        if r['t'] - lock.get(r['m'], 0) < 120_000: continue
        k = at(r['path'], L)
        if not k: continue
        ask, bid = k[3], k[2]; fol = ((bid + ask) / 2 - r['k0']) / r['move']; edge = r['m1'] - ask - kfee(ask)
        if fol > maxfol or edge < thr or ask - bid > 0.04 or ask > 0.90: continue
        lock[r['m']] = r['t']; ex = None
        for p in r['path']:
            if p[0] >= L + 1500 and p[2] is not None and p[2] - r['k0'] >= r['move'] - 0.01:
                q = at(r['path'], p[0] + 1000); ex = (q or p)[2]; why = 'caught up'; break
        if ex is None:
            q = at(r['path'], L + 300_000); ex = q[2] if q else bid; why = 'gave up'
        cost = ask + kfee(ask)
        T.append(dict(m=r['m'], sport=r['sport'], sell=(ex - kfee(ex) - cost) / cost, hold=(r['won'] - cost) / cost, edge=edge, ask=ask, cap=(k[4] or 0) * ask, why=why,
                      lock=(r['pm_bid'] - fee(r['pm_bid']) - cost) / (cost + 1 - r['pm_bid'])))
    return T
byg = lambda x: (lambda d: ([d[r['m']].append(r) for r in x], d)[1])(defaultdict(list))
def show(name, T):
    if len(T) < 8: print(f"  {name:34s} trades {len(T)}"); return
    c1 = boot(byg(T), lambda z: st.mean(r['sell'] for r in z) if z else None); c2 = boot(byg(T), lambda z: st.mean(r['hold'] for r in z) if z else None)
    print(f"  {name:34s} trades {len(T):4d} ({len(T)/days:4.1f} a day) | room at the buy {st.mean(r['edge'] for r in T)*100:4.1f}c | sold on catch-up or after 5 min: {sum(r['sell'] > 0 for r in T)/len(T)*100:3.0f}% won, {st.mean(r['sell'] for r in T)*100:+6.1f}% a $ ({c1[0]*100:+4.0f}..{c1[1]*100:+4.0f}), caught up {sum(r['why']=='caught up' for r in T)/len(T)*100:3.0f}%"
          f" | held to the result {st.mean(r['hold'] for r in T)*100:+6.1f}% ({c2[0]*100:+4.0f}..{c2[1]*100:+4.0f}) | hedged on Polymarket at once {st.mean(r['lock'] for r in T)*100:+5.1f}% | $ at the price, median {st.median(r['cap'] for r in T):,.0f}")
for L in (300, 1000, 2000, 4000):
    show(f"order lands {L/1000:.1f} s after the jump", sim(L))
print("  looser: any room above 1c")
for L in (1000, 2000): show(f"   {L/1000:.1f} s, 1c+ room", sim(L, thr=0.01))
T = sim(1000)
if len(T) >= 8: print("  by sport (1.0 s):", {s: (len(x), f"{st.mean(r['sell'] for r in x)*100:+.0f}%") for s, x in ((s, [r for r in T if r['sport'] == s]) for s in ('basketball', 'cs2', 'football', 'hockey', 'dota2', 'valorant', 'tennis')) if len(x) >= 3})
